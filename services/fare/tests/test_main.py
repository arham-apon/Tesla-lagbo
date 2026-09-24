"""6.7.7: the real app.main.app with its lifespan: consumer registered, outbox relay publishing, health, and the
money side of the whole evening end to end (estimate -> Trip's events -> settled -> fares, wallets, earnings)."""
import asyncio
import types

import httpx
import pytest
import respx
from alembic import command

from tesla_common.db import Database

from app import main
from app.routers import driver, fares, internal, wallet
from app.seed import seed
from conftest import GATEWAY, JASHIM, MATCHING, NUSRAT, RAFIQ, alembic_config, as_user
from test_settlement import cancelled, completed


class FakeBus:
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


def use(monkeypatch, database, redis=None, matching_http=None):
    """Point main and the routers (which import these at load time) at the test's database, Redis, Matching."""
    monkeypatch.setattr(main, "db", database)
    for module in (fares, wallet, driver, internal):
        monkeypatch.setattr(module, "db", database)
    if redis is not None:
        monkeypatch.setattr(main, "redis", redis)
        for module in (fares, internal):
            monkeypatch.setattr(module, "redis", redis)
    if matching_http is not None:
        monkeypatch.setattr(main, "matching_http", matching_http)
        for module in (fares, internal):
            monkeypatch.setattr(module, "matching_http", matching_http)


@pytest.fixture
async def service(db, redis, matching_http, bus, monkeypatch):
    use(monkeypatch, db, redis, matching_http)
    with respx.mock(base_url=MATCHING, assert_all_called=False) as m:
        m.get("/internal/zones/distance", params={"from": "BANANI", "to": "MOHAKHALI"}).mock(
            return_value=httpx.Response(200, json={"distance_m": 3500}))
        m.get("/internal/zones/distance", params={"from": "BANANI", "to": "GULSHAN_1"}).mock(
            return_value=httpx.Response(200, json={"distance_m": 2000}))
        async with main.app.router.lifespan_context(main.app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url="http://fare") as c:
                yield c


async def published(bus, key, count=1, timeout=5.0) -> list[dict]:
    """Wait for the outbox relay (every 0.5 s) to publish `count` events with this routing key."""
    for _ in range(int(timeout / 0.05)):
        found = [env["data"] for k, env in bus.published if k == key]
        if len(found) >= count:
            return found
        await asyncio.sleep(0.05)
    raise AssertionError(f"{key} not published; got {[k for k, _ in bus.published]}")


async def test_startup_registers_the_consumer(service, bus):
    assert {q: b for q, (b, _) in bus.queues.items()} == {
        "fare.ride-lifecycle": ["trip.ride.completed", "trip.ride.cancelled"]}


async def test_all_routes_are_mounted(service):
    assert {"/fares/estimate", "/fares/rides/{ride_id}", "/wallet", "/wallet/topup", "/driver/earnings",
            "/internal/quotes", "/internal/quotes/{quote_id}", "/health"} <= set(main.app.openapi()["paths"])


async def test_health(service, bus):
    resp = await service.get("/health")
    assert resp.status_code == 200 and resp.json()["checks"] == {"db": "ok", "redis": "ok", "rabbitmq": "ok"}
    bus.connection.is_closed = True
    resp = await service.get("/health")
    assert resp.status_code == 503 and resp.json()["checks"]["rabbitmq"].startswith("fail")


async def test_the_evening_in_money(service, bus, db):
    """Seeded wallets -> Nusrat and Rafiq get quotes -> both ride pooled in Bullet -> Trip's completed events ->
    fares settled and published -> everyone sees the right numbers."""
    await seed(db)
    _, on_event = bus.queues["fare.ride-lifecycle"]
    qn = (await service.post("/fares/estimate", json={"pickup_zone": "BANANI", "dropoff_zone": "MOHAKHALI",
                                                      "seats": 1}, headers=as_user(NUSRAT))).json()
    qr = (await service.post("/fares/estimate", json={"pickup_zone": "BANANI", "dropoff_zone": "GULSHAN_1",
                                                      "seats": 1}, headers=as_user(RAFIQ))).json()
    await on_event(completed("ride-rafiq", qr["quote_id"], RAFIQ, method="CASH"))   # Gulshan 1 first
    await on_event(completed("ride-nusrat", qn["quote_id"], NUSRAT))                 # then Mohakhali

    settled = await published(bus, "fare.ride.settled", count=2)
    assert [(e["ride_id"], e["total_poysha"], e["payment_method"], e["payment_status"]) for e in settled] == \
           [("ride-rafiq", 5400, "CASH", "PAID"), ("ride-nusrat", 7200, "WALLET", "PAID")]

    mine = (await service.get("/fares/rides/ride-nusrat", headers=as_user(NUSRAT))).json()
    assert (mine["total_poysha"], mine["pool_discount_poysha"]) == (7200, 1050)
    assert (await service.get("/wallet", headers=as_user(NUSRAT))).json()["balance_poysha"] == 42800
    jashim = as_user(JASHIM, "DRIVER")
    assert (await service.get("/wallet", headers=jashim)).json()["balance_poysha"] == 7200
    assert (await service.get("/driver/earnings", headers=jashim)).json() == \
           {"rides": 2, "total_poysha": 12600, "cash_poysha": 5400, "wallet_poysha": 7200}


async def test_a_cancelled_ride_voids_its_quote_for_trip(service, bus):
    _, on_event = bus.queues["fare.ride-lifecycle"]
    q = (await service.post("/internal/quotes", json={"pickup_zone": "BANANI", "dropoff_zone": "MOHAKHALI",
                                                      "seats": 1, "passenger_id": NUSRAT}, headers=GATEWAY)).json()
    await on_event(cancelled(q["quote_id"]))
    assert (await service.get(f"/internal/quotes/{q['quote_id']}", headers=GATEWAY)).status_code == 404


async def test_shutdown_closes_the_bus_and_stops_the_relay(db, redis, bus, monkeypatch):
    use(monkeypatch, db, redis)
    async with main.app.router.lifespan_context(main.app):
        assert "run_outbox_relay" in {t.get_coro().__name__ for t in asyncio.all_tasks()}
    assert bus.connection.is_closed
    await asyncio.sleep(0)
    assert "run_outbox_relay" not in {t.get_coro().__name__ for t in asyncio.all_tasks() if not t.done()}


async def test_refuses_to_start_without_tables(tmp_path, bus, monkeypatch):
    empty = Database(str(tmp_path / "empty.db"))
    use(monkeypatch, empty)
    with pytest.raises(RuntimeError, match="alembic upgrade head"):
        async with main.app.router.lifespan_context(main.app):
            pass
    assert bus.connection is None  # it never got as far as connecting
    await empty.dispose()


@pytest.fixture
def no_tariff_db_path(tmp_path):
    path = tmp_path / "no-tariff.db"
    command.upgrade(alembic_config(path), "0001")  # tables, but not the tariff seed (sync: Alembic runs its own loop)
    return path


async def test_refuses_to_start_without_a_tariff(no_tariff_db_path, bus, monkeypatch):
    database = Database(str(no_tariff_db_path))
    use(monkeypatch, database)
    with pytest.raises(RuntimeError, match="no active tariff"):
        async with main.app.router.lifespan_context(main.app):
            pass
    await database.dispose()
