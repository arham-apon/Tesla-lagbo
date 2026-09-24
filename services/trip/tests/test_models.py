"""5.3: the database itself refuses overbooking, impossible states and broken links, whatever the code does."""
import pytest
from sqlalchemy import delete, select, update
from sqlalchemy.exc import IntegrityError

from app.models import Pool, PoolWaypoint, RideOffer, RideStatusHistory
from conftest import KARIM, NUSRAT, RAFIQ, add, pool, ride, shift


async def fails(db, *rows) -> None:
    with pytest.raises(IntegrityError):
        await add(db, *rows)


# ---- driver_shifts ----------------------------------------------------------------------------------------------

@pytest.mark.parametrize("capacity, ok", [(0, False), (1, True), (6, True), (7, False)])
async def test_shift_capacity(db, capacity, ok):
    if ok:
        await add(db, shift(seat_capacity=capacity))
    else:
        await fails(db, shift(seat_capacity=capacity))


# ---- pools ------------------------------------------------------------------------------------------------------

async def test_new_pool_defaults(db):
    p = pool()
    await add(db, shift(), p)
    assert (p.status, p.occupied_seats, p.version) == ("FORMING", 0, 1)


async def test_pool_needs_a_known_driver(db):
    await fails(db, pool())  # no driver_shifts row (foreign keys are on in tesla_common)


@pytest.mark.parametrize("occupied, max_capacity", [(-1, 4), (5, 4), (1, 0), (7, 7)])
async def test_pool_seat_rules(db, occupied, max_capacity):
    await fails(db, shift(), pool(occupied_seats=occupied, max_capacity=max_capacity))


async def test_pool_status_must_be_known(db):
    await fails(db, shift(), pool(status="FULL"))  # "full" is derived, never stored


async def test_bullet_can_never_be_overbooked(db):
    """Bullet (4 seats) has 3 taken. Adding 2 more fails in the database itself, even with no version check."""
    p = pool(occupied_seats=3)
    await add(db, shift(), p)
    with pytest.raises(IntegrityError):
        async with db.rw.begin() as s:
            await s.execute(update(Pool).where(Pool.id == p.id).values(occupied_seats=Pool.occupied_seats + 2))
    async with db.ro() as s:
        assert (await s.get(Pool, p.id)).occupied_seats == 3


async def test_one_live_pool_per_driver(db):
    await add(db, shift(), pool())
    await fails(db, pool(status="IN_PROGRESS"))


async def test_finished_pools_do_not_count(db):
    await add(db, shift(), pool(status="COMPLETED"), pool(status="CANCELLED"), pool(status="COMPLETED"), pool())


async def test_two_drivers_each_with_a_live_pool(db):
    await add(db, shift(), shift(KARIM), pool(), pool(KARIM))


# ---- ride_requests ----------------------------------------------------------------------------------------------

async def test_new_ride_defaults(db):
    r = ride()
    await add(db, r)
    assert (r.status, r.pool_id, r.version) == ("REQUESTED", None, 1)


@pytest.mark.parametrize("bad", [
    {"seats": 0}, {"seats": 7},
    {"dropoff_zone": "BANANI"},                       # pickup = drop-off
    {"status": "ARRIVED"}, {"payment_method": "BKASH"},
    {"estimated_fare_poysha": -1},
])
async def test_ride_rules(db, bad):
    await fails(db, ride(**bad))


@pytest.mark.parametrize("status", ["MATCHED", "DRIVER_ARRIVED", "STARTED", "COMPLETED"])
async def test_matched_or_later_needs_a_pool(db, status):
    await fails(db, ride(status=status))


@pytest.mark.parametrize("status", ["REQUESTED", "CANCELLED"])
async def test_no_pool_needed_before_matching_or_when_cancelled(db, status):
    await add(db, ride(status=status))


async def test_ride_pool_must_exist(db):
    await fails(db, ride(status="MATCHED", pool_id="no-such-pool"))


async def test_one_active_ride_per_passenger(db):
    await add(db, ride())
    await fails(db, ride())  # the double-tap on "Request"


async def test_finished_rides_do_not_count(db):
    await add(db, ride(status="CANCELLED"), ride(status="CANCELLED"), ride())


async def test_different_passengers_each_with_an_active_ride(db):
    await add(db, ride(), ride(RAFIQ, passenger_name="Rafiq"))


async def test_pool_with_riders_cannot_be_deleted(db):
    p = pool()
    await add(db, shift(), p, ride(status="MATCHED", pool_id=p.id))
    with pytest.raises(IntegrityError):
        async with db.rw.begin() as s:
            await s.execute(delete(Pool).where(Pool.id == p.id))


# ---- pool_waypoints ---------------------------------------------------------------------------------------------

async def seeded(db):
    p = pool()
    r = ride(status="MATCHED", pool_id=p.id)
    await add(db, shift(), p, r)
    return p, r


def stop(p, r, seq, kind="PICKUP", zone="BANANI"):
    return PoolWaypoint(pool_id=p.id, ride_request_id=r.id, seq=seq, kind=kind, zone=zone)


async def test_waypoints_ordered(db):
    p, r = await seeded(db)
    await add(db, stop(p, r, 1), stop(p, r, 2, "DROPOFF", "MOHAKHALI"))
    async with db.ro() as s:
        rows = (await s.execute(select(PoolWaypoint.seq, PoolWaypoint.kind).order_by(PoolWaypoint.seq))).all()
    assert rows == [(1, "PICKUP"), (2, "DROPOFF")]


@pytest.mark.parametrize("seq, kind", [(0, "PICKUP"), (1, "STOP")])
async def test_waypoint_rules(db, seq, kind):
    p, r = await seeded(db)
    await fails(db, stop(p, r, seq, kind))


async def test_two_stops_cannot_share_a_place_in_line(db):
    p, r = await seeded(db)
    await add(db, stop(p, r, 1))
    await fails(db, stop(p, r, 1, "DROPOFF", "MOHAKHALI"))


# ---- ride_status_history and ride_offers -----------------------------------------------------------------------

@pytest.mark.parametrize("role, ok", [("PASSENGER", True), ("DRIVER", True), ("SYSTEM", True), ("ADMIN", False)])
async def test_history_actor(db, role, ok):
    r = ride()
    await add(db, r)
    row = RideStatusHistory(ride_id=r.id, to_status="REQUESTED", actor_id=NUSRAT, actor_role=role)
    if ok:
        await add(db, row)
        assert row.at is not None
    else:
        await fails(db, row)


async def test_history_needs_a_real_ride(db):
    await fails(db, RideStatusHistory(ride_id="nope", to_status="REQUESTED", actor_id=NUSRAT, actor_role="PASSENGER"))


async def test_offer_rules(db):
    r = ride()
    await add(db, r, RideOffer(ride_id=r.id, driver_id="driver-jashim", distance_m=900))
    await fails(db, RideOffer(ride_id=r.id, driver_id="driver-jashim", distance_m=900))  # offered twice
    await fails(db, RideOffer(ride_id=r.id, driver_id=KARIM, distance_m=1200, status="MAYBE"))
    async with db.ro() as s:
        assert (await s.get(RideOffer, (r.id, "driver-jashim"))).status == "OFFERED"

