"""5.5 / 5.9.4: requesting a ride, auto-joining a pool (compare-and-set), offers, and a driver accepting."""
import pytest
from sqlalchemy import select

from tesla_common.errors import DomainError

from app.models import Pool, PoolWaypoint, RideOffer, RideRequest, RideStatusHistory
from app.pooling import NEW_RIDE, JoinResult, accept_offer, open_pool_snapshot, request_ride, try_join
from app.schemas import RideCreate
from conftest import (AS_JASHIM, AS_KARIM, AS_NUSRAT, AS_RAFIQ, JASHIM, KARIM, RAFIQ, FakeFare, FakeMatching, add,
                      bullet_with_nusrat, events, rafiq_plan, ride, shift)

TO_GULSHAN = RideCreate(pickup_zone="BANANI", dropoff_zone="GULSHAN_1", seats=1)
TO_MOHAKHALI = RideCreate(pickup_zone="BANANI", dropoff_zone="MOHAKHALI", seats=1)


async def get(db, model, key):
    async with db.ro() as s:
        return await s.get(model, key)


async def stops(db, pool_id) -> list[tuple]:
    async with db.ro() as s:
        rows = (await s.execute(select(PoolWaypoint).where(PoolWaypoint.pool_id == pool_id)
                                .order_by(PoolWaypoint.seq))).scalars().all()
    return [(w.seq, w.kind, w.zone, w.ride_request_id) for w in rows]


async def rafiq_requested(db) -> str:
    r = ride(RAFIQ, passenger_name="Rafiq", dropoff_zone="GULSHAN_1")
    await add(db, r)
    return r.id


# ---- the snapshot Trip sends to Matching -----------------------------------------------------------------------

async def test_snapshot_shows_bullet_as_matching_expects_it(db):
    pool_id, nusrat = await bullet_with_nusrat(db)
    assert await open_pool_snapshot(db.ro, "BANANI", 1) == [{
        "pool_id": pool_id, "driver_id": JASHIM, "pickup_zone": "BANANI", "remaining_seats": 3, "version": 1,
        "stops": [{"ride_id": nusrat, "kind": "PICKUP", "zone": "BANANI", "done": False},
                  {"ride_id": nusrat, "kind": "DROPOFF", "zone": "MOHAKHALI", "done": False}]}]


async def test_snapshot_leaves_out_pools_that_cannot_take_the_rider(db):
    pool_id, _ = await bullet_with_nusrat(db)
    assert await open_pool_snapshot(db.ro, "MIRPUR", 1) == []    # other pickup zone
    assert await open_pool_snapshot(db.ro, "BANANI", 4) == []    # 3 seats left, 4 asked
    assert len(await open_pool_snapshot(db.ro, "BANANI", 3)) == 1
    async with db.rw.begin() as s:
        (await s.get(Pool, pool_id)).status = "IN_PROGRESS"      # closed to new riders
    assert await open_pool_snapshot(db.ro, "BANANI", 1) == []


# ---- try_join: the compare-and-set ----------------------------------------------------------------------------

async def test_no_options_means_none(db):
    assert await try_join(db.rw, "any", 1, []) is JoinResult.NONE


async def test_rafiq_joins_bullet(db):
    pool_id, nusrat = await bullet_with_nusrat(db)
    rafiq = await rafiq_requested(db)
    result = await try_join(db.rw, rafiq, 1, [{"pool_id": pool_id, "version": 1, "plan": rafiq_plan(nusrat)}])
    assert result is JoinResult.JOINED

    p, r = await get(db, Pool, pool_id), await get(db, RideRequest, rafiq)
    assert (p.occupied_seats, p.version, p.status) == (2, 2, "FORMING")
    assert (r.status, r.pool_id, r.version) == ("MATCHED", pool_id, 2)
    # Matching's plan, with "__new__" replaced by Rafiq's real ride id: G1 before M.
    assert await stops(db, pool_id) == [(1, "PICKUP", "BANANI", nusrat), (2, "PICKUP", "BANANI", rafiq),
                                        (3, "DROPOFF", "GULSHAN_1", rafiq), (4, "DROPOFF", "MOHAKHALI", nusrat)]
    async with db.ro() as s:
        h = (await s.execute(select(RideStatusHistory).where(RideStatusHistory.ride_id == rafiq))).scalars().one()
    assert (h.from_status, h.to_status, h.actor_role, h.reason) == ("REQUESTED", "MATCHED", "SYSTEM",
                                                                    "auto_joined_pool")
    (k1, matched), (k2, updated) = (await events(db))[-2:]
    assert (k1, matched["ride_id"], matched["joined_existing_pool"], matched["vehicle_nickname"]) == \
           ("trip.ride.matched", rafiq, True, "Bullet")
    assert (k2, updated["pool_id"], updated["driver_id"], updated["status"], updated["occupied_seats"]) == \
           ("trip.pool.updated", pool_id, JASHIM, "FORMING", 2)
    assert set(updated["member_passenger_ids"]) == {AS_NUSRAT.user_id, RAFIQ}
    assert NEW_RIDE not in str(updated)


async def test_stale_version_changes_nothing(db):
    pool_id, nusrat = await bullet_with_nusrat(db)
    rafiq = await rafiq_requested(db)
    before = await events(db)
    result = await try_join(db.rw, rafiq, 1, [{"pool_id": pool_id, "version": 99, "plan": rafiq_plan(nusrat)}])
    assert result is JoinResult.STALE
    assert (await get(db, Pool, pool_id)).occupied_seats == 1
    assert (await get(db, RideRequest, rafiq)).status == "REQUESTED"
    assert len(await stops(db, pool_id)) == 2 and await events(db) == before


async def test_no_joining_once_the_car_has_left(db):
    """Nusrat is already in the car (IN_PROGRESS). Even with the right version, Rafiq can't be added."""
    pool_id, nusrat = await bullet_with_nusrat(db)
    rafiq = await rafiq_requested(db)
    async with db.rw.begin() as s:
        (await s.get(Pool, pool_id)).status = "IN_PROGRESS"
    result = await try_join(db.rw, rafiq, 1, [{"pool_id": pool_id, "version": 1, "plan": rafiq_plan(nusrat)}])
    assert result is JoinResult.STALE
    assert (await get(db, Pool, pool_id)).occupied_seats == 1


async def test_next_option_is_tried_when_the_first_is_stale(db):
    pool_id, nusrat = await bullet_with_nusrat(db)
    rafiq = await rafiq_requested(db)
    opts = [{"pool_id": "gone", "version": 1, "plan": []},
            {"pool_id": pool_id, "version": 1, "plan": rafiq_plan(nusrat)}]
    assert await try_join(db.rw, rafiq, 1, opts) is JoinResult.JOINED


async def test_ride_cancelled_while_matching_rolls_the_seat_back(db):
    pool_id, nusrat = await bullet_with_nusrat(db)
    rafiq = await rafiq_requested(db)
    async with db.rw.begin() as s:
        (await s.get(RideRequest, rafiq)).status = "CANCELLED"
    with pytest.raises(DomainError) as e:
        await try_join(db.rw, rafiq, 1, [{"pool_id": pool_id, "version": 1, "plan": rafiq_plan(nusrat)}])
    assert e.value.code == "RIDE_NO_LONGER_REQUESTED"
    p = await get(db, Pool, pool_id)
    assert (p.occupied_seats, p.version) == (1, 1)


# ---- request_ride: quote -> ride -> join or offers ------------------------------------------------------------

async def test_request_auto_joins_an_open_pool(db):
    pool_id, _ = await bullet_with_nusrat(db)
    fare, matching = FakeFare(), FakeMatching()
    ride_id = await request_ride(AS_RAFIQ, TO_GULSHAN, ro=db.ro, rw=db.rw, fare=fare, matching=matching,
                                 request_id="req-1")
    r = await get(db, RideRequest, ride_id)
    assert (r.status, r.pool_id, r.passenger_name) == ("MATCHED", pool_id, "Rafiq")
    assert (r.quote_id, r.solo_distance_m, r.estimated_fare_poysha, r.estimated_pooled_fare_poysha) == \
           ("quote-1", 3500, 11000, 8800)
    assert matching.payloads[0]["open_pools"][0]["pool_id"] == pool_id  # the snapshot really was sent
    async with db.ro() as s:
        hist = (await s.execute(select(RideStatusHistory.to_status, RideStatusHistory.actor_role)
                                .where(RideStatusHistory.ride_id == ride_id).order_by(RideStatusHistory.id))).all()
    assert hist == [("REQUESTED", "PASSENGER"), ("MATCHED", "SYSTEM")]
    assert "trip.ride.requested" not in [k for k, _ in await events(db)]  # joined: no offers went out


async def test_request_uses_the_passengers_own_quote(db):
    fare = FakeFare()
    ride_id = await request_ride(AS_NUSRAT, TO_MOHAKHALI.model_copy(update={"quote_id": "q-app"}), ro=db.ro,
                                 rw=db.rw, fare=fare, matching=FakeMatching(), request_id=None)
    assert (await get(db, RideRequest, ride_id)).quote_id == "q-app"


async def test_no_pool_means_offers_to_nearby_drivers(db):
    matching = FakeMatching(candidates=[(JASHIM, 800), (KARIM, 2100)])
    ride_id = await request_ride(AS_NUSRAT, TO_MOHAKHALI, ro=db.ro, rw=db.rw, fare=FakeFare(), matching=matching,
                                 request_id=None)
    assert (await get(db, RideRequest, ride_id)).status == "REQUESTED"
    async with db.ro() as s:
        offers = (await s.execute(select(RideOffer.driver_id, RideOffer.distance_m, RideOffer.status)
                                  .order_by(RideOffer.distance_m))).all()
    assert offers == [(JASHIM, 800, "OFFERED"), (KARIM, 2100, "OFFERED")]
    key, data = (await events(db))[-1]
    assert (key, data["ride_id"], data["candidate_driver_ids"], data["estimated_fare_poysha"]) == \
           ("trip.ride.requested", ride_id, [JASHIM, KARIM], 11000)


async def test_second_active_ride_refused(db):
    kw = dict(ro=db.ro, rw=db.rw, fare=FakeFare(), matching=FakeMatching(), request_id=None)
    await request_ride(AS_NUSRAT, TO_MOHAKHALI, **kw)
    with pytest.raises(DomainError) as e:
        await request_ride(AS_NUSRAT, TO_GULSHAN, **kw)
    assert (e.value.code, e.value.status) == ("ACTIVE_RIDE_EXISTS", 409)


async def test_stale_answer_is_re_evaluated(db):
    await bullet_with_nusrat(db)
    matching = FakeMatching(stale_times=1)  # first answer is out of date, second is fresh
    ride_id = await request_ride(AS_RAFIQ, TO_GULSHAN, ro=db.ro, rw=db.rw, fare=FakeFare(), matching=matching,
                                 request_id=None)
    assert len(matching.payloads) == 2 and (await get(db, RideRequest, ride_id)).status == "MATCHED"


async def test_gives_up_joining_after_three_stale_answers(db):
    await bullet_with_nusrat(db)
    matching = FakeMatching(candidates=[(KARIM, 1500)], stale_times=99)
    ride_id = await request_ride(AS_RAFIQ, TO_GULSHAN, ro=db.ro, rw=db.rw, fare=FakeFare(), matching=matching,
                                 request_id=None)
    assert len(matching.payloads) == 3
    assert (await get(db, RideRequest, ride_id)).status == "REQUESTED"
    assert (await get(db, RideOffer, (ride_id, KARIM))).status == "OFFERED"


# ---- accept_offer: a driver starts a new pool -----------------------------------------------------------------

async def test_jashim_accepts_nusrat(db):
    pool_id, nusrat = await bullet_with_nusrat(db)
    p, r = await get(db, Pool, pool_id), await get(db, RideRequest, nusrat)
    assert (p.driver_id, p.vehicle_nickname, p.max_capacity, p.occupied_seats, p.pickup_zone, p.status) == \
           (JASHIM, "Bullet", 4, 1, "BANANI", "FORMING")
    assert (r.status, r.pool_id) == ("MATCHED", pool_id)
    assert (await get(db, RideOffer, (nusrat, JASHIM))).status == "ACCEPTED"
    assert await stops(db, pool_id) == [(1, "PICKUP", "BANANI", nusrat), (2, "DROPOFF", "MOHAKHALI", nusrat)]
    (k1, matched), (k2, updated) = (await events(db))[-2:]
    assert (k1, matched["joined_existing_pool"], matched["driver_id"]) == ("trip.ride.matched", False, JASHIM)
    assert (k2, updated["status"]) == ("trip.pool.updated", "FORMING")


async def refused(db, driver, ride_id, code):
    with pytest.raises(DomainError) as e:
        await accept_offer(db.rw, driver, ride_id)
    assert e.value.code == code
    return e.value


async def test_no_offer_no_accept(db):
    r = ride()
    await add(db, shift(), r)
    err = await refused(db, AS_JASHIM, r.id, "OFFER_NOT_FOUND")
    assert err.status == 404


async def test_declined_offer_cannot_be_accepted(db):
    r = ride()
    await add(db, shift(), r, RideOffer(ride_id=r.id, driver_id=JASHIM, distance_m=800, status="DECLINED"))
    await refused(db, AS_JASHIM, r.id, "OFFER_NOT_FOUND")


async def test_offline_driver_cannot_accept(db):
    r = ride()
    await add(db, shift(is_online=False), r, RideOffer(ride_id=r.id, driver_id=JASHIM, distance_m=800))
    await refused(db, AS_JASHIM, r.id, "DRIVER_OFFLINE")


async def test_car_too_small(db):
    r = ride(seats=5)
    await add(db, shift(), r, RideOffer(ride_id=r.id, driver_id=JASHIM, distance_m=800))
    await refused(db, AS_JASHIM, r.id, "VEHICLE_TOO_SMALL")
    assert (await get(db, RideRequest, r.id)).status == "REQUESTED"


async def test_driver_with_a_live_pool_cannot_start_another(db):
    await bullet_with_nusrat(db)
    r = ride(RAFIQ, passenger_name="Rafiq")
    await add(db, r, RideOffer(ride_id=r.id, driver_id=JASHIM, distance_m=800))
    await refused(db, AS_JASHIM, r.id, "DRIVER_HAS_LIVE_POOL")
    assert (await get(db, RideOffer, (r.id, JASHIM))).status == "OFFERED"


async def test_second_driver_is_too_late(db):
    pool_id, nusrat = await bullet_with_nusrat(db)
    await add(db, shift(KARIM, driver_name="Karim", vehicle_nickname="Toofan"),
              RideOffer(ride_id=nusrat, driver_id=KARIM, distance_m=1500))
    await refused(db, AS_KARIM, nusrat, "RIDE_NO_LONGER_AVAILABLE")
    async with db.ro() as s:
        pools = (await s.execute(select(Pool.driver_id))).scalars().all()
    assert pools == [JASHIM]  # Karim's half-made pool was rolled back
    assert (await get(db, RideOffer, (nusrat, KARIM))).status == "OFFERED"
