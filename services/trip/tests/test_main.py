"""5.8: the real app.main.app with its lifespan: consumers registered, outbox relay publishing, sweeper running,
health, and the whole evening end to end (event in -> HTTP -> events out)."""
import asyncio
import types
from datetime import datetime

import httpx
import pytest

from tesla_common.db import Database

from app import main
from app.routers import driver, internal, passenger
from conftest import AS_JASHIM, AS_NUSRAT, JASHIM, as_user
from test_consumers import ONLINE, envelope


class FakeBus:
    """Records queues and published events instead of talking to RabbitMQ."""
    def __init__(self):
        self.connection = None
        self.queues, self.published = {}, []

    async def connect(self, prefetch: int = 20) -> None:
        self.connection = types.SimpleNamespace(is_closed=False)

    async def consume(self, queue, bindings, handler, **kw):
        self.queues[queue] = (bindings, handler)

    async def publish_envelope(self, routing_key, env):
        self.published.append((routing_key, env))

    async def close(self) -> None:
        self.connection.is_closed = True


@pytest.fixture
def bus(monkeypatch):
    b = FakeBus()
    monkeypatch.setattr(main, "bus", b)
    return b


@pytest.fixture
async def service(db, bus, fare, matching, monkeypatch):
    monkeypatch.setattr(main, "db", db)
    for module in (passenger, driver, internal):
        monkeypatch.setattr(module, "db", db)
    monkeypatch.setattr(passenger, "fare_client", fare)
    monkeypatch.setattr(passenger, "matching_client", matching)
    async with main.app.router.lifespan_context(main.app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url="http://trip") as c:
            yield c


async def published(bus, key: str, count: int = 1, timeout: float = 5.0) -> list[dict]:
    """Wait for the outbox relay (every 0.5 s) to publish `count` events with this routing key."""
    for _ in range(int(timeout / 0.05)):
        found = [env["data"] for k, env in bus.published if k == key]
        if len(found) >= count:
            return found
        await asyncio.sleep(0.05)
    raise AssertionError(f"{key} not published; got {[k for k, _ in bus.published]}")


async def test_startup_registers_both_consumers(service, bus):
    assert {q: b for q, (b, _) in bus.queues.items()} == {
        "trip.driver-shift": ["identity.driver.*"], "trip.fare-settled": ["fare.ride.settled"]}


async def test_all_routes_are_mounted(service):
    paths = set(main.app.openapi()["paths"])
    assert {"/rides", "/rides/{ride_id}", "/rides/{ride_id}/cancel", "/driver/offers",
            "/driver/offers/{ride_id}/accept", "/driver/offers/{ride_id}/decline", "/driver/pool", "/driver/pools",
            "/driver/rides/{ride_id}/arrive", "/driver/rides/{ride_id}/start", "/driver/rides/{ride_id}/complete",
            "/driver/rides/{ride_id}/cancel", "/internal/drivers/{driver_id}/live-pool", "/health"} <= paths


async def test_health(service, bus):
    resp = await service.get("/health")
    assert resp.status_code == 200 and resp.json()["checks"] == {"db": "ok", "rabbitmq": "ok"}
    bus.connection.is_closed = True
    resp = await service.get("/health")
    assert resp.status_code == 503 and resp.json()["checks"]["rabbitmq"].startswith("fail")


async def test_the_whole_evening(service, bus, matching):
    """Identity says Jashim is online -> Nusrat requests -> the offer goes out -> Jashim accepts -> he drives her ->
    Fare settles -> Nusrat sees her final fare. Every event leaves through the outbox relay."""
    _, on_shift = bus.queues["trip.driver-shift"]
    await on_shift(envelope("identity.driver.online", ONLINE))

    matching.candidates = [(JASHIM, 800)]
    ride = (await service.post("/rides", json={"pickup_zone": "BANANI", "dropoff_zone": "MOHAKHALI", "seats": 1},
                               headers=as_user(AS_NUSRAT))).json()
    assert ride["status"] == "REQUESTED"
    (requested,) = await published(bus, "trip.ride.requested")
    assert requested["candidate_driver_ids"] == [JASHIM]

    jashim = as_user(AS_JASHIM)
    assert [o["ride_id"] for o in (await service.get("/driver/offers", headers=jashim)).json()] == [ride["id"]]
    pool = (await service.post(f"/driver/offers/{ride['id']}/accept", headers=jashim)).json()
    assert (await service.get(f"/internal/drivers/{JASHIM}/live-pool", headers=jashim)).json() == \
           {"pool_id": pool["id"]}
    for action in ("arrive", "start", "complete"):
        assert (await service.post(f"/driver/rides/{ride['id']}/{action}", headers=jashim)).status_code == 200

    (completed,) = await published(bus, "trip.ride.completed")
    assert (completed["ride_id"], completed["pooled"]) == (ride["id"], False)
    pool_events = await published(bus, "trip.pool.updated", count=4)  # accept, arrive, start, complete
    assert [e["status"] for e in pool_events] == ["FORMING", "FORMING", "IN_PROGRESS", "COMPLETED"]
    # the last one is what Matching frees Jashim on (4.7)

    _, on_settled = bus.queues["trip.fare-settled"]
    await on_settled(envelope("fare.ride.settled", {
        "fare_id": "f-1", "ride_id": ride["id"], "passenger_id": AS_NUSRAT.user_id, "driver_id": JASHIM,
        "base_poysha": 5000, "distance_charge_poysha": 5500, "pool_discount_poysha": 0, "total_poysha": 10500,
        "payment_method": "CASH", "payment_status": "PAID"}))
    mine = (await service.get(f"/rides/{ride['id']}", headers=as_user(AS_NUSRAT))).json()
    assert (mine["status"], mine["final_fare_poysha"], mine["payment_status"]) == ("COMPLETED", 10500, "PAID")
    assert [h["to_status"] for h in mine["history"]] == ["REQUESTED", "MATCHED", "DRIVER_ARRIVED", "STARTED",
                                                        "COMPLETED"]


async def test_events_leave_in_the_order_they_happened(service, bus, db):
    _, on_shift = bus.queues["trip.driver-shift"]
    await on_shift(envelope("identity.driver.online", ONLINE))
    body = {"pickup_zone": "BANANI", "dropoff_zone": "MOHAKHALI", "seats": 1}
    ride_id = (await service.post("/rides", json=body, headers=as_user(AS_NUSRAT))).json()["id"]
    await service.post(f"/rides/{ride_id}/cancel", json={}, headers=as_user(AS_NUSRAT))
    await published(bus, "trip.ride.cancelled")
    keys = [k for k, _ in bus.published]
    assert keys.index("trip.ride.requested") < keys.index("trip.ride.cancelled")
    times = [datetime.fromisoformat(env["occurred_at"].removesuffix("Z")) for _, env in bus.published]
    assert times == sorted(times)


async def test_shutdown_closes_the_bus_and_stops_the_background_tasks(db, bus, monkeypatch):
    monkeypatch.setattr(main, "db", db)
    async with main.app.router.lifespan_context(main.app):
        running = {t.get_coro().__name__ for t in asyncio.all_tasks()}
        assert {"run_outbox_relay", "expire_stale_requests"} <= running
    assert bus.connection.is_closed
    await asyncio.sleep(0)
    running = {t.get_coro().__name__ for t in asyncio.all_tasks() if not t.done()}
    assert not {"run_outbox_relay", "expire_stale_requests"} & running


async def test_refuses_to_start_without_tables(tmp_path, bus, monkeypatch):
    empty = Database(str(tmp_path / "empty.db"))
    monkeypatch.setattr(main, "db", empty)
    with pytest.raises(RuntimeError, match="alembic upgrade head"):
        async with main.app.router.lifespan_context(main.app):
            pass
    assert bus.connection is None  # it never got as far as connecting
    await empty.dispose()

