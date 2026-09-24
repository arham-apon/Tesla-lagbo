"""7.7: the real app.main.app with its lifespan: both queues registered, the location relay running, health, and
the evening end to end (events in -> sockets and inbox; Jashim's GPS -> the pool's passengers)."""
import asyncio
import json
import logging
import types

import fakeredis
import httpx
import pytest

from tesla_common.db import Database

from app import main
from app.connections import ConnectionManager
from app.routers import inbox as inbox_router
from conftest import JASHIM, NUSRAT, RAFIQ, as_user
from test_connections import FakeSocket
from test_routing import POOL, R_N, R_R, evening, pool_updated


class FakeBus:
    def __init__(self):
        self.connection = None
        self.durable, self.broadcast = {}, []

    async def connect(self, prefetch: int = 20) -> None:
        self.connection = types.SimpleNamespace(is_closed=False)

    async def consume(self, queue, bindings, handler, **kw):
        self.durable[queue] = (bindings, handler)

    async def consume_broadcast(self, bindings, handler):
        self.broadcast.append((bindings, handler))

    async def close(self) -> None:
        self.connection.is_closed = True


@pytest.fixture
def env(db, keys, monkeypatch):
    """main pointed at the test database, fakeredis, a fresh ConnectionManager, a fake bus and the test key."""
    bus, redis, manager = FakeBus(), fakeredis.aioredis.FakeRedis(decode_responses=True), ConnectionManager()
    for name, value in {"bus": bus, "db": db, "redis": redis, "manager": manager,
                        "public_key": lambda: keys[1]}.items():
        monkeypatch.setattr(main, name, value)
    monkeypatch.setattr(inbox_router, "db", db)
    return types.SimpleNamespace(bus=bus, redis=redis, manager=manager)


@pytest.fixture
async def service(env):
    async with main.app.router.lifespan_context(main.app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app),
                                     base_url="http://notification") as c:
            yield c


async def both(env, event_):
    """What RabbitMQ does: each event to the durable inbox queue AND to this copy's broadcast queue."""
    _, persist = env.bus.durable["notification.inbox"]
    ((_, push),) = env.bus.broadcast
    await persist(event_)
    await push(event_)


async def test_startup_registers_both_queues(service, env):
    assert list(env.bus.durable) == ["notification.inbox"] and len(env.bus.broadcast) == 1
    assert env.bus.durable["notification.inbox"][0] == env.bus.broadcast[0][0] == \
           ["trip.ride.*", "trip.pool.updated", "fare.ride.settled"]


async def test_routes_are_mounted(service):
    assert {"/notifications", "/notifications/{notification_id}/read", "/health"} <= \
           set(main.app.openapi()["paths"])
    assert main.app.url_path_for("ws_endpoint") == "/ws"  # WebSocket routes aren't in the OpenAPI schema


async def test_health(service, env):
    resp = await service.get("/health")
    assert resp.status_code == 200 and resp.json()["checks"] == {"db": "ok", "redis": "ok", "rabbitmq": "ok"}
    env.bus.connection.is_closed = True
    assert (await service.get("/health")).status_code == 503


async def test_token_redaction_is_switched_on(service):
    for name in ("uvicorn.error", "uvicorn.access"):
        assert main.REDACT in logging.getLogger(name).filters


async def test_the_evening_end_to_end(service, env):
    """Nusrat's phone is open; Rafiq's is off. Every event goes through both queues, as RabbitMQ delivers it."""
    nusrat_phone, jashim_phone = FakeSocket(), FakeSocket()
    await env.manager.connect(NUSRAT, nusrat_phone)
    await env.manager.connect(JASHIM, jashim_phone)
    for ev in evening():
        await both(env, ev)

    # live: Nusrat got her four messages as they happened; Jashim got his pool updates and both fares
    assert [m["type"] for m in nusrat_phone.sent] == ["ride.matched", "ride.status", "ride.status", "fare.settled"]
    assert [m["type"] for m in jashim_phone.sent].count("pool.updated") == 4

    # Rafiq's phone was off: he catches up from the inbox, with exactly what he'd have got live
    caught_up = (await service.get("/notifications", headers=as_user(RAFIQ))).json()
    assert [n["type"] for n in caught_up] == ["ride.matched", "ride.status", "ride.status", "fare.settled"]
    assert caught_up[-1]["payload"]["data"]["total_poysha"] == 5400
    assert all(n["payload"]["data"]["ride_id"] == R_R for n in caught_up)

    # and the same messages Nusrat saw live are in her inbox too
    assert [n["payload"] for n in (await service.get("/notifications", headers=as_user(NUSRAT))).json()] == \
           nusrat_phone.sent


async def test_the_car_moves_on_nusrats_map(service, env):
    phone = FakeSocket()
    await env.manager.connect(NUSRAT, phone)
    await both(env, pool_updated([NUSRAT]))  # she's in Bullet
    for _ in range(200):  # the relay subscribes at startup; give it up to 2 s (fail, don't hang, if it never does)
        if await env.redis.pubsub_numpat():
            break
        await asyncio.sleep(0.01)
    assert await env.redis.pubsub_numpat(), "location relay never subscribed"
    await env.redis.publish(f"loc:pool:{POOL}", json.dumps({"pool_id": POOL, "driver_id": JASHIM, "lat": 23.79,
                                                            "lng": 90.40, "zone": "BANANI", "ts": "t"}))
    for _ in range(100):
        if any(m["type"] == "vehicle.location" for m in phone.sent):
            break
        await asyncio.sleep(0.02)
    assert [m for m in phone.sent if m["type"] == "vehicle.location"] == [
        {"type": "vehicle.location", "data": {"lat": 23.79, "lng": 90.40, "zone": "BANANI", "ts": "t"}}]


async def test_redelivery_saves_once(service, env):
    ev = evening()[0]
    await both(env, ev)
    await both(env, ev)
    assert len((await service.get("/notifications", headers=as_user(NUSRAT))).json()) == 1


async def test_shutdown_stops_the_relay_and_closes_the_bus(env):
    async with main.app.router.lifespan_context(main.app):
        assert "relay_locations" in {t.get_coro().__name__ for t in asyncio.all_tasks()}
    assert env.bus.connection.is_closed
    await asyncio.sleep(0)
    assert "relay_locations" not in {t.get_coro().__name__ for t in asyncio.all_tasks() if not t.done()}


async def test_refuses_to_start_without_tables(tmp_path, env, monkeypatch):
    empty = Database(str(tmp_path / "empty.db"))
    monkeypatch.setattr(main, "db", empty)
    with pytest.raises(RuntimeError, match="alembic upgrade head"):
        async with main.app.router.lifespan_context(main.app):
            pass
    assert env.bus.connection is None
    await empty.dispose()


async def test_refuses_to_start_without_the_public_key(env, monkeypatch):
    def missing():
        raise FileNotFoundError("/run/keys/jwt_public.pem")

    monkeypatch.setattr(main, "public_key", missing)
    with pytest.raises(RuntimeError, match="JWT_PUBLIC_KEY_PATH"):
        async with main.app.router.lifespan_context(main.app):
            pass
    assert env.bus.connection is None
