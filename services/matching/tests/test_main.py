"""4.8 steps 5-6: the real app.main.app: lifespan (zones, Redis, bus, consumer), health, end-to-end flow."""
import types

import fakeredis
import httpx
import pytest
from alembic import command

from tesla_common.db import Database

from app import main
from conftest import GATEWAY, alembic_config, as_user


class FakeBus:
    def __init__(self):
        self.connection = None
        self.consumed = []

    async def connect(self, prefetch: int = 20) -> None:
        self.connection = types.SimpleNamespace(is_closed=False)

    async def consume(self, queue, bindings, handler, **kw):
        self.consumed.append((queue, bindings, handler))

    async def close(self) -> None:
        self.connection.is_closed = True


@pytest.fixture
def redis_server():
    return fakeredis.FakeServer()


@pytest.fixture
def fake_env(monkeypatch, redis_server):
    bus, redis = FakeBus(), fakeredis.aioredis.FakeRedis(server=redis_server, decode_responses=True)
    monkeypatch.setattr(main, "bus", bus)
    monkeypatch.setattr(main, "Redis", types.SimpleNamespace(from_url=lambda *a, **kw: redis))
    return bus, redis


@pytest.fixture
def no_zones_db_path(tmp_path):
    path = tmp_path / "no-zones.db"
    command.upgrade(alembic_config(path), "0001")  # tables, but the seed migration not run
    return path


@pytest.fixture
async def service(db, fake_env, monkeypatch):
    monkeypatch.setattr(main, "db", db)
    bus, redis = fake_env
    async with main.app.router.lifespan_context(main.app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url="http://matching") as c:
            yield c, bus, redis


async def test_startup_loads_zones_and_starts_the_consumer(service):
    _, bus, _ = service
    assert len(main.app.state.zones) == 9 and main.app.state.dist.get("BANANI", "MOHAKHALI") == 3500
    [(queue, bindings, _)] = bus.consumed
    assert queue == "matching.fleet-state" and bindings == ["identity.driver.*", "trip.pool.updated"]


async def test_banani_story_through_the_real_app(service):
    client, bus, _ = service
    handler = bus.consumed[0][2]
    await handler({"event_id": "e1", "event_type": "identity.driver.online", "occurred_at": "2026-09-24T08:40:00Z",
                   "producer": "identity-service", "version": 1, "data": {"driver_id": "jashim"}})
    ping = await client.post("/driver/location", json={"lat": 23.7940, "lng": 90.4070}, headers=as_user("jashim"))
    assert ping.status_code == 202 and ping.json() == {"zone": "BANANI"}
    resp = await client.post("/internal/match/evaluate", headers=GATEWAY,
                             json={"pickup_zone": "BANANI", "dropoff_zone": "MOHAKHALI", "seats": 1})
    body = resp.json()
    assert body["solo_distance_m"] == 3500 and [c["driver_id"] for c in body["candidate_drivers"]] == ["jashim"]


async def test_all_routes_mounted(service):
    assert {"/zones", "/driver/location", "/internal/zones/distance", "/internal/match/evaluate", "/health"} <= \
           set(main.app.openapi()["paths"])


async def test_health_ok(service):
    client, _, _ = service
    resp = await client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok", "checks": {"db": "ok", "redis": "ok", "rabbitmq": "ok"}}


async def test_health_degraded_when_broker_gone(service):
    client, bus, _ = service
    bus.connection.is_closed = True
    resp = await client.get("/health")
    assert resp.status_code == 503 and resp.json()["checks"]["rabbitmq"].startswith("fail")


async def test_health_degraded_when_redis_gone(service, redis_server):
    client, _, _ = service
    redis_server.connected = False
    resp = await client.get("/health")
    assert resp.status_code == 503 and resp.json()["checks"]["redis"].startswith("fail")


async def test_shutdown_closes_the_bus(db, fake_env, monkeypatch):
    monkeypatch.setattr(main, "db", db)
    bus, _ = fake_env
    async with main.app.router.lifespan_context(main.app):
        pass
    assert bus.connection.is_closed


async def test_refuses_to_start_without_zones(no_zones_db_path, fake_env, monkeypatch):
    empty = Database(str(no_zones_db_path))
    monkeypatch.setattr(main, "db", empty)
    with pytest.raises(RuntimeError, match="alembic upgrade head"):
        async with main.app.router.lifespan_context(main.app):
            pass
    await empty.dispose()
