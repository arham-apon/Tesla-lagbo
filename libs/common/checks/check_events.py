"""Step 1.4.4 — prove retry + DLQ and the outbox relay against a real RabbitMQ.

Needs RabbitMQ:  docker compose up -d rabbitmq
Run:             python libs/common/checks/check_events.py

Part 1: a handler that always raises. Expected: 1 try + 3 retries (each ~5 s apart,
        via `<queue>.retry`), then the message is parked in `<queue>.dlq`.
Part 2: emit() into an SQLite outbox -> run_outbox_relay publishes it -> a consumer
        receives it; first_time() drops a duplicate of the same event_id.
"""
import asyncio
import os
import tempfile
import time

from sqlalchemy.orm import DeclarativeBase

from tesla_common.db import Database
from tesla_common.events import Bus, OutboxMixin, ProcessedEventMixin, emit, first_time, run_outbox_relay
from tesla_common.timeutil import new_id

URL = os.getenv("RABBITMQ_URL", "amqp://tesla:change-me@localhost:5672/")


async def check_retry_and_dlq() -> None:
    queue = f"check.retry.{new_id()[:8]}"
    bus = Bus(URL)
    await bus.connect()
    t0 = time.perf_counter()
    calls: list[float] = []

    async def always_fails(envelope: dict) -> None:
        calls.append(time.perf_counter() - t0)
        print(f"  {calls[-1]:5.1f}s  handler call #{len(calls)} -> raising")
        raise RuntimeError("boom")

    await bus.consume(queue, ["check.failing"], always_fails)
    await bus.publish_envelope("check.failing", {"event_id": new_id(), "event_type": "check.failing", "data": {}})

    dlq = await bus.channel.declare_queue(f"{queue}.dlq", durable=True, passive=True)
    msg = None
    for _ in range(40):
        await asyncio.sleep(0.5)
        msg = await dlq.get(no_ack=True, fail=False)
        if msg:
            break

    for name in (queue, f"{queue}.retry", f"{queue}.dlq"):
        await bus.channel.queue_delete(name)
    await bus.close()

    assert len(calls) == 4, f"expected 4 handler calls, got {len(calls)}"
    gaps = [b - a for a, b in zip(calls, calls[1:])]
    assert all(4.5 < g < 7 for g in gaps), f"retry gaps not ~5 s: {gaps}"
    assert msg is not None, "message never reached the DLQ"
    print(f"  DLQ headers: x-attempt={msg.headers['x-attempt']} x-error={msg.headers['x-error']!r}")
    assert msg.headers["x-attempt"] == 4
    print("OK: 1 try + 3 retries ~5 s apart, then parked in .dlq")


class Base(DeclarativeBase):
    pass


class Outbox(OutboxMixin, Base):
    pass


class ProcessedEvent(ProcessedEventMixin, Base):
    pass


async def check_outbox_and_idempotency() -> None:
    db = Database(os.path.join(tempfile.mkdtemp(), "outbox.db"))
    async with db.rw_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    queue = f"check.outbox.{new_id()[:8]}"
    bus = Bus(URL)
    await bus.connect()
    received: list[dict] = []
    applied: list[str] = []

    async def handler(envelope: dict) -> None:
        received.append(envelope)
        async with db.rw.begin() as s:
            if await first_time(s, ProcessedEvent, envelope["event_id"]):
                applied.append(envelope["event_id"])

    await bus.consume(queue, ["trip.ride.completed"], handler)

    # The business write and the event are committed in ONE transaction.
    async with db.rw.begin() as s:
        event_id = emit(s, Outbox, "trip-service", "trip.ride.completed", {"ride_id": "r-1", "passenger_id": "nusrat"})

    stop = asyncio.Event()
    relay = asyncio.create_task(run_outbox_relay(db.ro, db.rw, Outbox, bus, stop, interval=0.2))
    for _ in range(25):
        await asyncio.sleep(0.2)
        if received:
            break

    # Simulate the relay crashing after publish but before marking published_at: a duplicate.
    await bus.publish_envelope("trip.ride.completed", received[0])
    await asyncio.sleep(1)

    stop.set()
    await relay
    async with db.ro() as s:
        row = (await s.execute(Outbox.__table__.select())).one()
    for name in (queue, f"{queue}.retry", f"{queue}.dlq"):
        await bus.channel.queue_delete(name)
    await bus.close()
    await db.dispose()

    assert received[0]["event_id"] == event_id and received[0]["data"]["passenger_id"] == "nusrat"
    assert row.published_at is not None, "relay did not mark the row published"
    assert len(received) == 2 and applied == [event_id], f"received={len(received)} applied={applied}"
    print(f"  delivered {len(received)} times, applied {len(applied)} time; outbox row published_at={row.published_at}")
    print("OK: outbox relay published the event; duplicate dropped by first_time()")


async def main() -> None:
    print("Part 1: retry + DLQ (takes ~15 s)")
    await check_retry_and_dlq()
    print("\nPart 2: outbox relay + idempotent consumer")
    await check_outbox_and_idempotency()


if __name__ == "__main__":
    asyncio.run(main())
