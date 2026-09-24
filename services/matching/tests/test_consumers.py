"""4.7: the matching.fleet-state consumer: routing keys, bindings, handling real emit() envelopes, failures."""
import json
from unittest.mock import patch

import fakeredis
import pytest

from tesla_common.events import emit

from app import consumers
from app.fleet import AVAILABLE, h

# Every routing key in the Part 0.4 registry.
REGISTRY_KEYS = ["identity.driver.online", "identity.driver.offline", "trip.ride.requested", "trip.ride.matched",
                 "trip.ride.status_changed", "trip.ride.cancelled", "trip.ride.completed", "trip.pool.updated",
                 "fare.ride.settled"]
WANTED = {"identity.driver.online", "identity.driver.offline", "trip.pool.updated"}


class _Capture:
    def __init__(self):
        self.rows = []

    def add(self, row):
        self.rows.append(row)


def envelope(routing_key: str, data: dict, at: str) -> dict:
    """An envelope made by the real tesla_common emit(), with its clock frozen at `at`."""
    from datetime import datetime
    session = _Capture()
    with patch("tesla_common.events.utcnow", return_value=datetime.fromisoformat(at)):
        emit(session, lambda **kw: kw, "test-producer", routing_key, data)
    return json.loads(session.rows[0]["payload"])


def topic_matches(pattern: str, key: str) -> bool:
    p, k = pattern.split("."), key.split(".")
    return len(p) == len(k) and all(a in ("*", b) for a, b in zip(p, k))


class FakeBus:
    def __init__(self):
        self.consumed = []

    async def consume(self, queue, bindings, handler, **kw):
        self.consumed.append((queue, bindings, handler))


# ---- queue + bindings -------------------------------------------------------------------------------------------

def test_bindings_receive_exactly_the_events_matching_needs():
    received = {k for k in REGISTRY_KEYS if any(topic_matches(b, k) for b in consumers.BINDINGS)}
    assert received == WANTED


async def test_start_declares_the_plans_queue(redis):
    bus = FakeBus()
    await consumers.start(bus, redis)
    [(queue, bindings, handler)] = bus.consumed
    assert queue == "matching.fleet-state" and bindings == ["identity.driver.*", "trip.pool.updated"]
    await handler(envelope("identity.driver.online", {"driver_id": "jashim"}, "2026-09-24T08:40:00"))
    assert await redis.smembers(AVAILABLE) == {"jashim"}


# ---- handling real envelopes --------------------------------------------------------------------------------------

async def test_identity_online_event_from_identity(redis):
    # The exact data Identity sends (Part 3.5); Matching only needs driver_id.
    data = {"driver_id": "jashim", "driver_name": "Jashim", "vehicle_id": "b111", "vehicle_nickname": "Bullet",
            "plate": "DHAKA-TESLA-11", "seat_capacity": 3}
    await consumers.handle(redis, envelope("identity.driver.online", data, "2026-09-24T08:40:00"))
    assert await redis.hget(h("jashim"), "online") == "1"


async def test_identity_offline_event_makes_him_unavailable(redis):
    await consumers.handle(redis, envelope("identity.driver.online", {"driver_id": "jashim"}, "2026-09-24T08:40:00"))
    await consumers.handle(redis, envelope("identity.driver.offline", {"driver_id": "jashim"}, "2026-09-24T17:00:00"))
    assert await redis.smembers(AVAILABLE) == set() and await redis.hget(h("jashim"), "online") == "0"


async def test_pool_lifecycle_from_trip_events(redis):
    await consumers.handle(redis, envelope("identity.driver.online", {"driver_id": "jashim"}, "2026-09-24T08:40:00"))
    pool = {"pool_id": "bullet-1", "driver_id": "jashim", "status": "FORMING", "occupied_seats": 1,
            "max_capacity": 3, "member_passenger_ids": ["nusrat"], "waypoints": []}
    await consumers.handle(redis, envelope("trip.pool.updated", pool, "2026-09-24T08:41:00"))
    assert await redis.smembers(AVAILABLE) == set()
    await consumers.handle(redis, envelope("trip.pool.updated", pool | {"status": "COMPLETED"},
                                           "2026-09-24T09:05:00.250000"))
    assert await redis.smembers(AVAILABLE) == {"jashim"}


async def test_whole_second_envelopes_from_real_emit_keep_order(redis):
    off = envelope("identity.driver.offline", {"driver_id": "jashim"}, "2026-09-24T08:41:05")
    on = envelope("identity.driver.online", {"driver_id": "jashim"}, "2026-09-24T08:41:05.120000")
    assert off["occurred_at"] == "2026-09-24T08:41:05.000000Z"  # emit() keeps the fraction on a whole second
    assert off["occurred_at"] < on["occurred_at"]                # so text order = time order
    await consumers.handle(redis, off)
    await consumers.handle(redis, on)
    assert await redis.smembers(AVAILABLE) == {"jashim"}


async def test_redelivered_event_changes_nothing(redis):
    ev = envelope("identity.driver.online", {"driver_id": "jashim"}, "2026-09-24T08:40:00")
    await consumers.handle(redis, ev)
    await redis.srem(AVAILABLE, "jashim")  # something else changed the set meanwhile
    await consumers.handle(redis, ev)      # at-least-once delivery: the same event again
    assert await redis.smembers(AVAILABLE) == set()  # ignored as already applied


async def test_other_event_types_are_ignored(redis):
    await consumers.handle(redis, envelope("trip.ride.completed", {"ride_id": "r1"}, "2026-09-24T08:40:00"))
    assert await redis.keys("*") == []


# ---- failures go to Bus retry / DLQ (Part 1) ----------------------------------------------------------------------

async def test_redis_down_raises_so_the_bus_retries():
    server = fakeredis.FakeServer()
    server.connected = False
    down = fakeredis.aioredis.FakeRedis(server=server, decode_responses=True)
    with pytest.raises(Exception):
        await consumers.handle(down, envelope("identity.driver.online", {"driver_id": "j"}, "2026-09-24T08:40:00"))


async def test_malformed_event_raises_so_it_ends_in_the_dlq(redis):
    with pytest.raises(KeyError):
        await consumers.handle(redis, envelope("trip.pool.updated", {"pool_id": "p"}, "2026-09-24T08:40:00"))
