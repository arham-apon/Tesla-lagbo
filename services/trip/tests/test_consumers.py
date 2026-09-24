"""5.7: Trip's two consumers: the driver-shift copy (from Identity) and final fares (from Fare)."""
import json
from datetime import datetime
from unittest.mock import patch

import pytest
from sqlalchemy import select

from tesla_common.events import emit

from app import consumers
from app.models import DriverShift, ProcessedEvent, RideRequest
from conftest import JASHIM, add, bullet_with_nusrat, ride

ONLINE = {"driver_id": JASHIM, "driver_name": "Jashim", "vehicle_id": "vehicle-1", "vehicle_nickname": "Bullet",
          "plate": "DHAKA-METRO-GA-11-2233", "seat_capacity": 4}  # exactly Identity's payload (registry 0.4)


class _Capture:
    def __init__(self):
        self.rows = []

    def add(self, row):
        self.rows.append(row)


def envelope(routing_key: str, data: dict, at: str = "2026-09-24T08:40:00") -> dict:
    """An envelope made by the real tesla_common emit(), with its clock frozen at `at`."""
    session = _Capture()
    with patch("tesla_common.events.utcnow", return_value=datetime.fromisoformat(at)):
        emit(session, lambda **kw: kw, "identity-service", routing_key, data)
    return json.loads(session.rows[0]["payload"])


async def get_shift(db):
    async with db.ro() as s:
        return await s.get(DriverShift, JASHIM)


# ---- driver shifts ----------------------------------------------------------------------------------------------

async def test_online_creates_the_shift(db):
    await consumers.on_driver_shift(db.rw, envelope("identity.driver.online", ONLINE))
    sh = await get_shift(db)
    assert (sh.driver_name, sh.vehicle_nickname, sh.seat_capacity, sh.is_online) == ("Jashim", "Bullet", 4, True)
    assert sh.state_ts == "2026-09-24T08:40:00.000000Z"


async def test_offline_then_online_in_another_car(db):
    await consumers.on_driver_shift(db.rw, envelope("identity.driver.online", ONLINE, "2026-09-24T08:00:00"))
    await consumers.on_driver_shift(db.rw, envelope("identity.driver.offline", {"driver_id": JASHIM},
                                                    "2026-09-24T12:00:00"))
    assert (await get_shift(db)).is_online is False
    await consumers.on_driver_shift(db.rw, envelope("identity.driver.online", ONLINE | {
        "vehicle_nickname": "Toofan", "seat_capacity": 6}, "2026-09-24T18:00:00"))
    sh = await get_shift(db)
    assert (sh.is_online, sh.vehicle_nickname, sh.seat_capacity) == (True, "Toofan", 6)


async def test_late_event_is_ignored(db):
    """Offline at 12:00 is delivered after online at 18:00 (e.g. it went round the retry queue)."""
    await consumers.on_driver_shift(db.rw, envelope("identity.driver.online", ONLINE, "2026-09-24T18:00:00"))
    await consumers.on_driver_shift(db.rw, envelope("identity.driver.offline", {"driver_id": JASHIM},
                                                    "2026-09-24T12:00:00"))
    assert (await get_shift(db)).is_online is True


async def test_whole_second_then_120ms_later(db):
    """The 4.5 bug, end to end: offline exactly on a second, online 120 ms later. Must end online."""
    await consumers.on_driver_shift(db.rw, envelope("identity.driver.online", ONLINE, "2026-09-24T08:00:00"))
    off = envelope("identity.driver.offline", {"driver_id": JASHIM}, "2026-09-24T08:41:05")
    on = envelope("identity.driver.online", ONLINE, "2026-09-24T08:41:05.120000")
    assert off["occurred_at"] == "2026-09-24T08:41:05.000000Z"
    await consumers.on_driver_shift(db.rw, off)
    await consumers.on_driver_shift(db.rw, on)
    assert (await get_shift(db)).is_online is True


async def test_redelivered_event_changes_nothing(db):
    ev = envelope("identity.driver.online", ONLINE)
    await consumers.on_driver_shift(db.rw, ev)
    async with db.rw.begin() as s:
        (await s.get(DriverShift, JASHIM)).is_online = False  # something newer happened meanwhile
    await consumers.on_driver_shift(db.rw, ev)                # RabbitMQ delivers the old one again
    assert (await get_shift(db)).is_online is False


async def test_offline_for_a_driver_trip_never_saw(db):
    await consumers.on_driver_shift(db.rw, envelope("identity.driver.offline", {"driver_id": JASHIM}))
    assert await get_shift(db) is None


async def test_other_identity_driver_events_do_not_mean_offline(db):
    """The fix: the queue takes identity.driver.*; the plan's code treated anything but "online" as "offline"."""
    await consumers.on_driver_shift(db.rw, envelope("identity.driver.online", ONLINE, "2026-09-24T08:00:00"))
    await consumers.on_driver_shift(db.rw, envelope("identity.driver.vehicle_changed", {"driver_id": JASHIM},
                                                    "2026-09-24T09:00:00"))
    sh = await get_shift(db)
    assert (sh.is_online, sh.state_ts) == (True, "2026-09-24T08:00:00.000000Z")


async def test_bad_event_raises_and_leaves_nothing(db):
    """Missing fields -> raise, so the bus retries and then dead-letters it. The dedupe row is rolled back too,
    so the retry is really processed."""
    ev = envelope("identity.driver.online", {"driver_id": JASHIM})  # no name, no vehicle
    with pytest.raises(KeyError):
        await consumers.on_driver_shift(db.rw, ev)
    async with db.ro() as s:
        assert await s.get(ProcessedEvent, ev["event_id"]) is None
    assert await get_shift(db) is None


# ---- fare settled -----------------------------------------------------------------------------------------------

def settled(ride_id: str, **kw) -> dict:
    return envelope("fare.ride.settled", {
        "fare_id": "fare-1", "ride_id": ride_id, "passenger_id": "p", "driver_id": JASHIM, "base_poysha": 5000,
        "distance_charge_poysha": 4000, "pool_discount_poysha": 1800, "total_poysha": 7200,
        "payment_method": "WALLET", "payment_status": "PAID"} | kw)


async def test_final_fare_is_stored(db):
    _, nusrat = await bullet_with_nusrat(db)
    await consumers.on_fare_settled(db.rw, settled(nusrat))
    async with db.ro() as s:
        r = await s.get(RideRequest, nusrat)
    assert (r.final_fare_poysha, r.payment_status) == (7200, "PAID")


async def test_failed_payment_is_stored_too(db):
    r = ride(status="CANCELLED")
    await add(db, r)
    await consumers.on_fare_settled(db.rw, settled(r.id, total_poysha=2000, payment_status="FAILED"))
    async with db.ro() as s:
        assert (await s.get(RideRequest, r.id)).payment_status == "FAILED"


async def test_settled_twice_is_harmless(db):
    _, nusrat = await bullet_with_nusrat(db)
    ev = settled(nusrat)
    await consumers.on_fare_settled(db.rw, ev)
    await consumers.on_fare_settled(db.rw, ev)
    async with db.ro() as s:
        assert len((await s.execute(select(ProcessedEvent))).scalars().all()) == 1


async def test_unknown_ride_is_not_an_error(db):
    await consumers.on_fare_settled(db.rw, settled("no-such-ride"))  # nothing to update, nothing to retry


# ---- wiring -----------------------------------------------------------------------------------------------------

class RecordingBus:
    def __init__(self):
        self.queues = {}

    async def consume(self, queue, bindings, handler, **kw):
        self.queues[queue] = (bindings, handler)


async def test_queues_and_bindings_match_the_plan(db):
    bus = RecordingBus()
    await consumers.start(bus, db.rw)
    assert {q: b for q, (b, _) in bus.queues.items()} == {
        "trip.driver-shift": ["identity.driver.*"], "trip.fare-settled": ["fare.ride.settled"]}
    _, handler = bus.queues["trip.driver-shift"]
    await handler(envelope("identity.driver.online", ONLINE))  # the registered handler really writes to our db
    assert (await get_shift(db)).is_online is True
