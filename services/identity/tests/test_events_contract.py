"""3.5: Identity's events match the Part 0.4 event registry exactly (Trip and Matching depend on these shapes)."""
import json
from datetime import datetime

import pytest
from sqlalchemy import select

from app.models import Driver, Outbox, User
from conftest import as_user

JASHIM = "11111111-1111-4111-8111-111111111111"
J = as_user(JASHIM, "DRIVER", "Jashim")
BULLET = {"nickname": "Bullet", "make": "Tesla", "model": "Model 3", "plate": "DHAKA-TESLA-11", "seat_capacity": 3}

# Part 0.4 registry
REGISTRY = {
    "identity.driver.online": {"driver_id", "driver_name", "vehicle_id", "vehicle_nickname", "plate", "seat_capacity"},
    "identity.driver.offline": {"driver_id"},
}
ENVELOPE = {"event_id", "event_type", "occurred_at", "producer", "version", "data"}
CONSUMER_BINDINGS = {"trip.driver-shift": "identity.driver.*", "matching.fleet-state": "identity.driver.*"}


def topic_matches(pattern: str, key: str) -> bool:
    """RabbitMQ topic rule for the patterns used here: '*' = exactly one word."""
    p, k = pattern.split("."), key.split(".")
    return len(p) == len(k) and all(a in ("*", b) for a, b in zip(p, k))


@pytest.fixture
async def envelopes(api, db):
    async with db.rw.begin() as s:
        s.add(User(id=JASHIM, full_name="Jashim", phone="01711000001", password_hash="x", role="DRIVER"))
        s.add(Driver(user_id=JASHIM, license_number="DK-0001"))
    await api.put("/drivers/me/vehicle", json=BULLET, headers=J)
    await api.post("/drivers/me/online", headers=J)
    await api.post("/drivers/me/offline", headers=J)
    async with db.ro() as s:
        rows = (await s.execute(select(Outbox).order_by(Outbox.id))).scalars().all()
    return [(r.routing_key, json.loads(r.payload)) for r in rows]


async def test_exactly_the_registry_events(envelopes):
    assert [rk for rk, _ in envelopes] == ["identity.driver.online", "identity.driver.offline"]


async def test_data_fields_match_registry(envelopes):
    for routing_key, env in envelopes:
        assert set(env["data"]) == REGISTRY[routing_key], routing_key


async def test_online_field_types(envelopes):
    data = envelopes[0][1]["data"]
    assert isinstance(data["seat_capacity"], int) and data["seat_capacity"] == 3
    assert all(isinstance(data[k], str) for k in REGISTRY["identity.driver.online"] - {"seat_capacity"})


async def test_envelope_shape(envelopes):
    for routing_key, env in envelopes:
        assert set(env) == ENVELOPE
        assert env["event_type"] == routing_key
        assert env["producer"] == "identity-service" and env["version"] == 1
        assert env["occurred_at"].endswith("Z") and datetime.fromisoformat(env["occurred_at"].removesuffix("Z"))
    assert len({env["event_id"] for _, env in envelopes}) == 2


@pytest.mark.parametrize("queue, pattern", CONSUMER_BINDINGS.items())
async def test_consumers_bindings_receive_both(envelopes, queue, pattern):
    assert all(topic_matches(pattern, rk) for rk, _ in envelopes), queue
