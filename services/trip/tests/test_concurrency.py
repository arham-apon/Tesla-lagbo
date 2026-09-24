"""5.5 (plan 8.5): real races on a real SQLite file (not :memory:, which would give each connection its own
database and hide the race). BEGIN IMMEDIATE + compare-and-set must let exactly one writer win."""
import asyncio

from sqlalchemy import func, select

from tesla_common.errors import DomainError

from app.models import Pool, RideOffer, RideRequest
from app.pooling import NEW_RIDE, JoinResult, accept_offer, try_join
from conftest import AS_JASHIM, AS_KARIM, JASHIM, KARIM, SHIRIN, add, pool, ride, shift


def option(pool_id: str) -> list[dict]:
    return [{"pool_id": pool_id, "version": 1, "plan": [
        {"ride_id": NEW_RIDE, "kind": "PICKUP", "zone": "BANANI"},
        {"ride_id": NEW_RIDE, "kind": "DROPOFF", "zone": "MOHAKHALI"}]}]


async def test_last_seat_goes_to_exactly_one_rider(db):
    """The plan's test: Bullet (3 seats, 2 taken). Nusrat and Shirin both try for the last seat at once."""
    p = pool(max_capacity=3, occupied_seats=2)
    nusrat, shirin = ride(), ride(SHIRIN, passenger_name="Shirin")
    await add(db, shift(seat_capacity=3), p, nusrat, shirin)

    results = await asyncio.gather(try_join(db.rw, nusrat.id, 1, option(p.id)),
                                   try_join(db.rw, shirin.id, 1, option(p.id)))

    assert sorted(r.value for r in results) == ["joined", "stale"]
    async with db.ro() as s:
        assert (await s.get(Pool, p.id)).occupied_seats == 3
        assert await s.scalar(select(func.count()).where(RideRequest.pool_id == p.id)) == 1


async def test_ten_riders_one_seat(db):
    p = pool(occupied_seats=3)
    riders = [ride(f"passenger-{i}", passenger_name=f"P{i}") for i in range(10)]
    await add(db, shift(), p, *riders)

    results = await asyncio.gather(*(try_join(db.rw, r.id, 1, option(p.id)) for r in riders))

    assert [r for r in results if r is JoinResult.JOINED] == [JoinResult.JOINED]
    async with db.ro() as s:
        assert (await s.get(Pool, p.id)).occupied_seats == 4


async def test_two_drivers_accept_the_same_ride(db):
    r = ride()
    await add(db, shift(), shift(KARIM, driver_name="Karim", vehicle_nickname="Toofan"), r,
              RideOffer(ride_id=r.id, driver_id=JASHIM, distance_m=800),
              RideOffer(ride_id=r.id, driver_id=KARIM, distance_m=900))

    results = await asyncio.gather(accept_offer(db.rw, AS_JASHIM, r.id), accept_offer(db.rw, AS_KARIM, r.id),
                                   return_exceptions=True)

    wins = [x for x in results if isinstance(x, str)]
    losses = [x for x in results if isinstance(x, DomainError)]
    assert len(wins) == 1 and [e.code for e in losses] == ["RIDE_NO_LONGER_AVAILABLE"]
    async with db.ro() as s:
        assert await s.scalar(select(func.count()).select_from(Pool)) == 1  # the loser's pool rolled back
        assert (await s.get(RideRequest, r.id)).pool_id == wins[0]
