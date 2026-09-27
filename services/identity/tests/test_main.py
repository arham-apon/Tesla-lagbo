"""3.6 step 6: the real app.main.app: lifespan, outbox relay -> bus, /health, routers mounted."""
import asyncio

import httpx
import pytest
from sqlalchemy import select

from app import main
from app.models import Outbox
from conftest import GATEWAY

JASHIM = {"full_name": "Jashim", "phone": "01711000001", "password": "Pool@1234", "role": "DRIVER",
          "license_number": "DK-0001"}
BULLET = {"nickname": "Bullet", "make": "Tesla", "model": "Model 3", "plate": "DHAKA-TESLA-11", "seat_capacity": 3}


class FakeBus:
    """Records what the outbox relay publishes, instead of talking to RabbitMQ."""

    def __init__(self):
        self.published: list[tuple[str, dict]] = []
        self.connection = None
        self.fail = False

    async def connect(self, prefetch: int = 20) -> None:
        self.connection = type("Conn", (), {"is_closed": False})()

    async def publish_envelope(self, routing_key: str, envelope: dict) -> None:
        if self.fail:
            raise ConnectionError("broker down")
        self.published.append((routing_key, envelope))

    async def close(self) -> None:
        self.connection.is_closed = True


@pytest.fixture
async def service(wired, db, redis, monkeypatch):
    """app.main.app with its lifespan running (relay task live), on the temp DB / fake Redis / fake bus."""
    bus = FakeBus()
    monkeypatch.setattr(main, "bus", bus)
    monkeypatch.setattr(main, "db", db)
    monkeypatch.setattr(main, "redis", redis)
    monkeypatch.setattr(main, "trip_client", wired)
    async with main.app.router.lifespan_context(main.app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url="http://identity") as c:
            yield c, bus


async def wait_for(predicate, timeout: float = 5.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        assert asyncio.get_running_loop().time() < deadline, "timed out"
        await asyncio.sleep(0.05)


async def jashim_online(client) -> dict:
    user = (await client.post("/auth/register", json=JASHIM, headers=GATEWAY)).json()
    j = GATEWAY | {"X-User-Id": user["id"], "X-User-Role": "DRIVER", "X-User-Name": "Jashim"}
    await client.put("/drivers/me/vehicle", json=BULLET, headers=j)
    assert (await client.post("/drivers/me/online", headers=j)).status_code == 200
    return j


async def test_online_event_reaches_the_bus_and_row_is_marked(service, db):
    client, bus = service
    await jashim_online(client)
    await wait_for(lambda: bus.published)
    [(routing_key, envelope)] = bus.published
    assert routing_key == "identity.driver.online" and envelope["data"]["vehicle_nickname"] == "Bullet"
    async def marked():
        async with db.ro() as s:
            return (await s.execute(select(Outbox))).scalar_one().published_at is not None
    for _ in range(100):
        if await marked():
            break
        await asyncio.sleep(0.05)
    assert await marked()


async def test_broker_outage_delays_but_never_loses_the_event(service, db):
    client, bus = service
    bus.fail = True
    await jashim_online(client)
    await asyncio.sleep(1.2)  # the relay retries every 0.5 s and keeps failing
    assert not bus.published
    async with db.ro() as s:
        assert (await s.execute(select(Outbox))).scalar_one().published_at is None  # still waiting, not lost
    bus.fail = False
    await wait_for(lambda: bus.published)
    assert bus.published[0][0] == "identity.driver.online"


async def test_health_ok(service):
    client, _ = service
    resp = await client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok", "checks": {"db": "ok", "rabbitmq": "ok", "redis": "ok"}}


async def test_health_degraded_when_broker_connection_closed(service):
    client, bus = service
    bus.connection.is_closed = True
    resp = await client.get("/health")
    assert resp.status_code == 503 and resp.json()["checks"]["rabbitmq"].startswith("fail")


async def test_all_routers_mounted(service):
    paths = set(main.app.openapi()["paths"])
    assert {"/auth/register", "/auth/login", "/auth/logout", "/users/me", "/drivers/me", "/drivers/me/vehicle",
            "/drivers/me/online", "/drivers/me/offline", "/internal/users/{user_id}", "/health"} <= paths


async def test_error_format_installed(service):
    client, _ = service
    resp = await client.post("/auth/login", json={"phone": "bad"}, headers=GATEWAY)
    assert resp.status_code == 422 and resp.json()["error"]["code"] == "VALIDATION_ERROR"
