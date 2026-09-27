"""5.5 (plan 8.5): Bullet's seats are always right: never over capacity, never negative, and always equal to the
seats of the riders actually in the car (or about to be)."""
import pytest
from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError

from app.lifecycle import transition
from app.models import ACTIVE_RIDE_STATUSES, Pool, RideRequest
from app.pooling import JoinResult, open_pool_snapshot, request_ride, try_join
from app.schemas import RideCreate
from conftest import (AS_JASHIM, AS_RAFIQ, AS_SHIRIN, FakeFare, FakeMatching, add, bullet_with_nusrat,
                      pool, ride, shift)


async def seats_add_up(db, pool_id) -> int:
    """occupied_seats must equal the seats of the pool's rides that haven't finished."""
    async with db.ro() as s:
        p = await s.get(Pool, pool_id)
        booked = await s.scalar(select(func.coalesce(func.sum(RideRequest.seats), 0)).where(
            RideRequest.pool_id == pool_id, RideRequest.status.in_(ACTIVE_RIDE_STATUSES)))
    assert p.occupied_seats == booked
    return booked


async def test_direct_overbooking_is_refused_by_the_database(db):
    p = pool(max_capacity=3, occupied_seats=3)
    await add(db, shift(seat_capacity=3), p)
    with pytest.raises(IntegrityError):
        async with db.rw.begin() as s:
            await s.execute(update(Pool).where(Pool.id == p.id).values(occupied_seats=4))


async def test_two_seats_do_not_fit_in_one(db):
    """Plan 8.5: try_join with 2 seats on a 3-seat car with 2 taken -> STALE, seats stay 2."""
    p = pool(max_capacity=3, occupied_seats=2)
    r = ride(seats=2)
    await add(db, shift(seat_capacity=3), p, r)
    plan = [{"ride_id": "__new__", "kind": "PICKUP", "zone": "BANANI"},
            {"ride_id": "__new__", "kind": "DROPOFF", "zone": "MOHAKHALI"}]
    assert await try_join(db.rw, r.id, 2, [{"pool_id": p.id, "version": 1, "plan": plan}]) is JoinResult.STALE
    async with db.ro() as s:
        assert (await s.get(Pool, p.id)).occupied_seats == 2


async def test_seats_through_a_whole_evening(db):
    """Fill Bullet to 4, see it disappear from the snapshot, free seats, fill again, finish. Check after every step."""
    pool_id, nusrat = await bullet_with_nusrat(db)
    kw = dict(ro=db.ro, rw=db.rw, fare=FakeFare(), matching=FakeMatching(), request_id=None)
    assert await seats_add_up(db, pool_id) == 1

    rafiq = await request_ride(AS_RAFIQ, RideCreate(pickup_zone="BANANI", dropoff_zone="GULSHAN_1", seats=2), **kw)
    assert await seats_add_up(db, pool_id) == 3
    shirin = await request_ride(AS_SHIRIN, RideCreate(pickup_zone="BANANI", dropoff_zone="GULSHAN_2", seats=1),
                                **kw)
    assert await seats_add_up(db, pool_id) == 4
    assert await open_pool_snapshot(db.ro, "BANANI", 1) == []  # full: not offered to anyone

    await transition(db.rw, rafiq, "CANCELLED", AS_RAFIQ)
    assert await seats_add_up(db, pool_id) == 2
    assert (await open_pool_snapshot(db.ro, "BANANI", 2))[0]["remaining_seats"] == 2  # open again

    for r in (nusrat, shirin):
        await transition(db.rw, r, "DRIVER_ARRIVED", AS_JASHIM)
        await transition(db.rw, r, "STARTED", AS_JASHIM)
    assert await seats_add_up(db, pool_id) == 2
    await transition(db.rw, shirin, "COMPLETED", AS_JASHIM)
    assert await seats_add_up(db, pool_id) == 1
    await transition(db.rw, nusrat, "COMPLETED", AS_JASHIM)
    assert await seats_add_up(db, pool_id) == 0
    async with db.ro() as s:
        assert (await s.get(Pool, pool_id)).status == "COMPLETED"


async def test_a_full_car_sends_the_next_rider_to_offers(db):
    pool_id, _ = await bullet_with_nusrat(db)
    kw = dict(ro=db.ro, rw=db.rw, fare=FakeFare(), matching=FakeMatching(), request_id=None)
    await request_ride(AS_RAFIQ, RideCreate(pickup_zone="BANANI", dropoff_zone="GULSHAN_1", seats=3), **kw)
    shirin = await request_ride(AS_SHIRIN, RideCreate(pickup_zone="BANANI", dropoff_zone="GULSHAN_2", seats=1), **kw)
    async with db.ro() as s:
        assert (await s.get(RideRequest, shirin)).status == "REQUESTED"
    assert await seats_add_up(db, pool_id) == 4


async def test_cancelling_a_request_touches_no_seats(db):
    pool_id, _ = await bullet_with_nusrat(db)
    r = ride(AS_RAFIQ.user_id, passenger_name="Rafiq", pickup_zone="MIRPUR")
    await add(db, r)
    await transition(db.rw, r.id, "CANCELLED", AS_RAFIQ)
    assert await seats_add_up(db, pool_id) == 1  # Nusrat's seat is untouched
