"""5.5: the sweeper cancels requests nobody accepted within RIDE_REQUEST_TTL_SECONDS (plan default 180 s)."""
import asyncio
from datetime import timedelta

from sqlalchemy import select

from tesla_common.timeutil import utcnow

from app.models import RideRequest, RideStatusHistory
from app.workers import expire_stale_requests
from conftest import RAFIQ, SHIRIN, add, bullet_with_nusrat, events, ride

TTL = 180


async def sweep_once(db) -> None:
    stop = asyncio.Event()
    task = asyncio.create_task(expire_stale_requests(db.ro, db.rw, TTL, stop, every=0.01))
    await asyncio.sleep(0.2)
    stop.set()
    await asyncio.wait_for(task, 1)


async def status(db, ride_id):
    async with db.ro() as s:
        r = await s.get(RideRequest, ride_id)
        return r.status, r.cancel_reason


async def test_old_request_is_cancelled_by_the_system(db):
    old = ride(created_at=utcnow() - timedelta(seconds=TTL + 5))
    await add(db, old)
    await sweep_once(db)
    assert await status(db, old.id) == ("CANCELLED", "NO_DRIVER_FOUND")
    async with db.ro() as s:
        h = (await s.execute(select(RideStatusHistory).where(RideStatusHistory.ride_id == old.id))).scalars().one()
    assert (h.from_status, h.to_status, h.actor_role, h.actor_id) == ("REQUESTED", "CANCELLED", "SYSTEM", "system")
    _, data = (await events(db))[-1]
    assert (data["cancelled_by"], data["reason"]) == ("SYSTEM", "NO_DRIVER_FOUND")


async def test_young_request_is_left_alone(db):
    young = ride(RAFIQ, passenger_name="Rafiq", created_at=utcnow() - timedelta(seconds=TTL - 30))
    await add(db, young)
    await sweep_once(db)
    assert await status(db, young.id) == ("REQUESTED", None)


async def test_matched_rides_are_never_expired(db):
    _, nusrat = await bullet_with_nusrat(db)
    async with db.rw.begin() as s:
        (await s.get(RideRequest, nusrat)).created_at = utcnow() - timedelta(hours=1)
    await sweep_once(db)
    assert (await status(db, nusrat))[0] == "MATCHED"


async def test_one_bad_ride_does_not_stop_the_rest(db):
    # Both are old; the sweeper must get through the whole batch.
    rides = [ride(p, passenger_name=p, created_at=utcnow() - timedelta(seconds=TTL + 60)) for p in (RAFIQ, SHIRIN)]
    await add(db, *rides)
    await sweep_once(db)
    assert [(await status(db, r.id))[0] for r in rides] == ["CANCELLED", "CANCELLED"]


async def test_survives_a_broken_database_and_stops_when_asked(db, caplog):
    async def broken():
        raise RuntimeError("disk gone")

    class BrokenRO:
        def __call__(self):
            return self

        async def __aenter__(self):
            await broken()

        async def __aexit__(self, *a):
            return False

    stop = asyncio.Event()
    task = asyncio.create_task(expire_stale_requests(BrokenRO(), db.rw, TTL, stop, every=0.01))
    await asyncio.sleep(0.1)
    assert not task.done()  # still looping
    stop.set()
    await asyncio.wait_for(task, 1)
    assert "sweeper iteration failed" in caplog.text
