import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from datetime import datetime

import aio_pika
from aio_pika import DeliveryMode, ExchangeType, Message
from aio_pika.abc import AbstractIncomingMessage
from sqlalchemy import DateTime, Integer, String, Text, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import Mapped, mapped_column

from .timeutil import new_id, utcnow

log = logging.getLogger("tesla.events")
EXCHANGE = "tesla.events"
Handler = Callable[[dict], Awaitable[None]]


class OutboxMixin:
    __tablename__ = "outbox"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    event_id: Mapped[str] = mapped_column(String(36), unique=True)
    routing_key: Mapped[str] = mapped_column(String(100))
    payload: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    published_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, index=True)


class ProcessedEventMixin:
    __tablename__ = "processed_events"
    event_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    processed_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


def emit(session: AsyncSession, outbox_model, producer: str, routing_key: str, data: dict) -> str:
    event_id = new_id()
    envelope = {
        "event_id": event_id,
        "event_type": routing_key,
        # Always 6 decimals: a bare isoformat() drops ".000000" on a whole second, and then
        # "08:41:05Z" sorts after "08:41:05.120000Z". Fixed width makes text order = time order.
        "occurred_at": utcnow().isoformat(timespec="microseconds") + "Z",
        "producer": producer,
        "version": 1,
        "data": data,
    }
    session.add(outbox_model(event_id=event_id, routing_key=routing_key, payload=json.dumps(envelope)))
    return event_id


async def first_time(session: AsyncSession, processed_model, event_id: str) -> bool:
    if await session.get(processed_model, event_id) is not None:
        return False
    session.add(processed_model(event_id=event_id))
    return True


class Bus:
    def __init__(self, url: str):
        self.url = url
        self.connection: aio_pika.abc.AbstractRobustConnection | None = None
        self.channel: aio_pika.abc.AbstractChannel | None = None
        self.exchange: aio_pika.abc.AbstractExchange | None = None

    async def connect(self, prefetch: int = 20) -> None:
        self.connection = await aio_pika.connect_robust(self.url)
        self.channel = await self.connection.channel(publisher_confirms=True)
        await self.channel.set_qos(prefetch_count=prefetch)
        self.exchange = await self.channel.declare_exchange(EXCHANGE, ExchangeType.TOPIC, durable=True)

    async def publish_envelope(self, routing_key: str, envelope: dict) -> None:
        await self.exchange.publish(
            Message(
                json.dumps(envelope).encode(),
                content_type="application/json",
                delivery_mode=DeliveryMode.PERSISTENT,
                message_id=envelope["event_id"],
            ),
            routing_key=routing_key,
        )

    async def consume(self, queue_name: str, bindings: list[str], handler: Handler,
                      max_retries: int = 3, retry_delay_ms: int = 5000) -> None:
        ch = self.channel
        await ch.declare_queue(f"{queue_name}.dlq", durable=True)
        await ch.declare_queue(
            f"{queue_name}.retry", durable=True,
            arguments={"x-message-ttl": retry_delay_ms,
                       "x-dead-letter-exchange": "",
                       "x-dead-letter-routing-key": queue_name},
        )
        queue = await ch.declare_queue(queue_name, durable=True)
        for rk in bindings:
            await queue.bind(self.exchange, routing_key=rk)

        async def on_message(msg: AbstractIncomingMessage) -> None:
            try:
                envelope = json.loads(msg.body)
            except json.JSONDecodeError:
                await self._reroute(msg, f"{queue_name}.dlq", attempt=0, error="invalid JSON")
                return
            try:
                await handler(envelope)
                await msg.ack()
            except Exception as exc:
                attempt = int((msg.headers or {}).get("x-attempt", 0)) + 1
                target = f"{queue_name}.retry" if attempt <= max_retries else f"{queue_name}.dlq"
                log.exception("handler failed", extra={"event_id": envelope.get("event_id")})
                await self._reroute(msg, target, attempt=attempt, error=str(exc))

        await queue.consume(on_message)

    async def consume_broadcast(self, bindings: list[str], handler: Handler) -> None:
        queue = await self.channel.declare_queue(exclusive=True, auto_delete=True)
        for rk in bindings:
            await queue.bind(self.exchange, routing_key=rk)

        async def on_message(msg: AbstractIncomingMessage) -> None:
            async with msg.process(requeue=False):
                await handler(json.loads(msg.body))

        await queue.consume(on_message)

    async def _reroute(self, msg: AbstractIncomingMessage, target: str, *, attempt: int, error: str) -> None:
        try:
            headers = dict(msg.headers or {}) | {
                "x-attempt": attempt,
                "x-error": error[:500],
                "x-original-routing-key": msg.routing_key or "",
            }
            await self.channel.default_exchange.publish(
                Message(msg.body, headers=headers, content_type="application/json",
                        delivery_mode=DeliveryMode.PERSISTENT, message_id=msg.message_id),
                routing_key=target,
            )
            await msg.ack()
        except Exception:
            log.exception("reroute failed; requeueing")
            await msg.nack(requeue=True)

    async def close(self) -> None:
        if self.connection:
            await self.connection.close()


async def run_outbox_relay(ro: async_sessionmaker, rw: async_sessionmaker, outbox_model, bus: Bus,
                           stop: asyncio.Event, interval: float = 0.5, batch: int = 100) -> None:
    while not stop.is_set():
        try:
            async with ro() as s:
                rows = (await s.execute(
                    select(outbox_model).where(outbox_model.published_at.is_(None))
                    .order_by(outbox_model.id).limit(batch)
                )).scalars().all()
            published: list[int] = []
            for row in rows:
                await bus.publish_envelope(row.routing_key, json.loads(row.payload))
                published.append(row.id)
            if published:
                async with rw.begin() as s:
                    await s.execute(update(outbox_model).where(outbox_model.id.in_(published))
                                    .values(published_at=utcnow()))
        except Exception:
            log.exception("outbox relay iteration failed")
        await asyncio.sleep(interval)
