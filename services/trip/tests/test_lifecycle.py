"""5.5 / 5.9.5: everything after booking: arrive, start, complete, cancel, and what each does to the pool."""
import pytest
from sqlalchemy import select

from tesla_common.errors import DomainError

from app.lifecycle import transition
from app.models import Pool, PoolWaypoint, RideOffer, RideRequest, RideStatusHistory
from app.pooling import JoinResult, accept_offer, try_join
from conftest import (AS_JASHIM, AS_NUSRAT, AS_RAFIQ, JASHIM, RAFIQ, add, bullet_with_nusrat, events, rafiq_plan,
                      ride)


async def get(db, model, key):
    async with db.ro() as s:
        return await s.get(model, key)


async def stops(db, pool_id) -> list[tuple]:
    async with db.ro() as s:
        rows = (await s.execute(select(PoolWaypoint).where(PoolWaypoint.pool_id == pool_id)
                                .order_by(PoolWaypoint.seq))).scalars().all()
    return [(w.seq, w.kind, w.zone, w.done_at is not None) for w in rows]


@pytest.fixture
async def bullet(db):
    """Bullet with Nusrat (Banani -> Mohakhali) and Rafiq (Banani -> Gulshan 1), both MATCHED. 2 of 4 seats."""
    pool_id, nusrat = await bullet_with_nusrat(db)
    r = ride(RAFIQ, passenger_name="Rafiq", dropoff_zone="GULSHAN_1")
    await add(db, r)
    assert await try_join(db.rw, r.id, 1, [{"pool_id": pool_id, "version": 1, "plan": rafiq_plan(nusrat)}]) \
        is JoinResult.JOINED
    return pool_id, nusrat, r.id


async def drive(db, ride_id, *targets):
    for t in targets:
        await transition(db.rw, ride_id, t, AS_JASHIM)


async def test_the_whole_story(db, bullet):
    pool_id, nusrat, rafiq = bullet
    await drive(db, nusrat, "DRIVER_ARRIVED")
    assert (await get(db, Pool, pool_id)).status == "FORMING"  # arriving doesn't close the pool
    await drive(db, nusrat, "STARTED")
    p = await get(db, Pool, pool_id)
    assert (p.status, p.started_at is not None) == ("IN_PROGRESS", True)  # first pickup closes it to joins
    await drive(db, rafiq, "DRIVER_ARRIVED", "STARTED")
    assert [d for *_, d in await stops(db, pool_id)] == [True, True, False, False]  # both pickups done

    await drive(db, rafiq, "COMPLETED")  # Gulshan 1 first
    p = await get(db, Pool, pool_id)
    assert (p.status, p.occupied_seats) == ("IN_PROGRESS", 1)
    assert await stops(db, pool_id) == [(1, "PICKUP", "BANANI", True), (2, "PICKUP", "BANANI", True),
                                        (3, "DROPOFF", "GULSHAN_1", True), (4, "DROPOFF", "MOHAKHALI", False)]

    await drive(db, nusrat, "COMPLETED")
    p = await get(db, Pool, pool_id)
    assert (p.status, p.occupied_seats, p.completed_at is not None) == ("COMPLETED", 0, True)

    completed = [d for k, d in await events(db) if k == "trip.ride.completed"]
    assert [(d["ride_id"], d["pooled"], d["co_rider_count"]) for d in completed] == [(rafiq, True, 1),
                                                                                     (nusrat, True, 1)]
    assert completed[0]["quote_id"] == "quote-1" and completed[0]["payment_method"] == "CASH"
    last_pool_event = [d for k, d in await events(db) if k == "trip.pool.updated"][-1]
    assert (last_pool_event["status"], last_pool_event["member_passenger_ids"]) == ("COMPLETED", [])


async def test_history_tells_the_story(db, bullet):
    _, nusrat, _ = bullet
    await drive(db, nusrat, "DRIVER_ARRIVED", "STARTED", "COMPLETED")
    async with db.ro() as s:
        rows = (await s.execute(select(RideStatusHistory).where(RideStatusHistory.ride_id == nusrat)
                                .order_by(RideStatusHistory.id))).scalars().all()
    # (The "-> REQUESTED" row is written by request_ride; this fixture inserts her ride directly.)
    assert [(h.from_status, h.to_status, h.actor_role, h.actor_id) for h in rows] == [
        ("REQUESTED", "MATCHED", "DRIVER", JASHIM),
        ("MATCHED", "DRIVER_ARRIVED", "DRIVER", JASHIM),
        ("DRIVER_ARRIVED", "STARTED", "DRIVER", JASHIM),
        ("STARTED", "COMPLETED", "DRIVER", JASHIM)]


async def test_arrive_and_start_are_announced(db, bullet):
    _, nusrat, _ = bullet
    await drive(db, nusrat, "DRIVER_ARRIVED")
    key, data = [e for e in await events(db) if e[0] == "trip.ride.status_changed"][-1]
    assert (data["from_status"], data["to_status"], data["actor_role"], data["driver_id"]) == \
           ("MATCHED", "DRIVER_ARRIVED", "DRIVER", JASHIM)


# ---- cancelling ------------------------------------------------------------------------------------------------

async def test_passenger_cannot_cancel_after_driver_arrived(db, bullet):
    pool_id, nusrat, _ = bullet
    await drive(db, nusrat, "DRIVER_ARRIVED")
    before = await events(db)
    with pytest.raises(DomainError) as e:
        await transition(db.rw, nusrat, "CANCELLED", AS_NUSRAT, "changed_plans")
    assert (e.value.code, e.value.status) == ("INVALID_TRANSITION", 409)
    assert (await get(db, RideRequest, nusrat)).status == "DRIVER_ARRIVED"
    assert (await get(db, Pool, pool_id)).occupied_seats == 2 and await events(db) == before


async def test_cancel_in_matched_frees_the_seat_and_the_stops(db, bullet):
    pool_id, nusrat, rafiq = bullet
    await transition(db.rw, rafiq, "CANCELLED", AS_RAFIQ, "changed_plans")
    r, p = await get(db, RideRequest, rafiq), await get(db, Pool, pool_id)
    assert (r.status, r.cancel_reason) == ("CANCELLED", "changed_plans")
    assert (p.status, p.occupied_seats) == ("FORMING", 1)  # Nusrat's pool carries on, open to new riders
    assert await stops(db, pool_id) == [(1, "PICKUP", "BANANI", False), (2, "DROPOFF", "MOHAKHALI", False)]
    key, data = [e for e in await events(db) if e[0] == "trip.ride.cancelled"][-1]
    assert (data["from_status"], data["cancelled_by"], data["pool_id"], data["quote_id"]) == \
           ("MATCHED", "PASSENGER", pool_id, "quote-1")


async def test_driver_no_show(db, bullet):
    pool_id, nusrat, _ = bullet
    await drive(db, nusrat, "DRIVER_ARRIVED")
    await transition(db.rw, nusrat, "CANCELLED", AS_JASHIM, "PASSENGER_NO_SHOW")
    assert (await get(db, RideRequest, nusrat)).cancel_reason == "PASSENGER_NO_SHOW"
    assert (await get(db, Pool, pool_id)).occupied_seats == 1
    _, data = [e for e in await events(db) if e[0] == "trip.ride.cancelled"][-1]
    assert (data["cancelled_by"], data["reason"]) == ("DRIVER", "PASSENGER_NO_SHOW")


async def test_last_rider_cancelling_dissolves_the_pool(db):
    pool_id, nusrat = await bullet_with_nusrat(db)
    await transition(db.rw, nusrat, "CANCELLED", AS_NUSRAT)
    p = await get(db, Pool, pool_id)
    assert (p.status, p.occupied_seats, p.completed_at is not None) == ("CANCELLED", 0, True)
    _, data = [e for e in await events(db) if e[0] == "trip.pool.updated"][-1]
    assert (data["status"], data["driver_id"]) == ("CANCELLED", JASHIM)  # Matching frees Jashim on this


async def test_a_driver_is_free_for_a_new_pool_after_completing(db):
    pool_id, nusrat = await bullet_with_nusrat(db)
    await drive(db, nusrat, "DRIVER_ARRIVED", "STARTED", "COMPLETED")
    r = ride(RAFIQ, passenger_name="Rafiq")
    await add(db, r, RideOffer(ride_id=r.id, driver_id=JASHIM, distance_m=500))
    new_pool = await accept_offer(db.rw, AS_JASHIM, r.id)
    assert new_pool != pool_id  # the one-live-pool rule only counts live pools


async def test_cancel_a_request_with_no_pool(db):
    r = ride()
    await add(db, r)
    await transition(db.rw, r.id, "CANCELLED", AS_NUSRAT, "changed_plans")
    keys = [k for k, _ in await events(db)]
    assert keys == ["trip.ride.cancelled"]  # no pool, so no pool event
    assert (await events(db))[0][1]["pool_id"] is None


# ---- the "pooled" flag Fare prices on ----------------------------------------------------------------------------

async def test_alone_in_the_car_is_not_pooled(db):
    _, nusrat = await bullet_with_nusrat(db)
    await drive(db, nusrat, "DRIVER_ARRIVED", "STARTED", "COMPLETED")
    _, data = [e for e in await events(db) if e[0] == "trip.ride.completed"][-1]
    assert (data["pooled"], data["co_rider_count"]) == (False, 0)


async def test_co_rider_who_cancelled_does_not_count(db, bullet):
    _, nusrat, rafiq = bullet
    await transition(db.rw, rafiq, "CANCELLED", AS_RAFIQ)
    await drive(db, nusrat, "DRIVER_ARRIVED", "STARTED", "COMPLETED")
    _, data = [e for e in await events(db) if e[0] == "trip.ride.completed"][-1]
    assert (data["pooled"], data["co_rider_count"]) == (False, 0)  # Nusrat pays the solo fare


# ---- things that must not happen -------------------------------------------------------------------------------

async def test_unknown_ride(db):
    with pytest.raises(DomainError) as e:
        await transition(db.rw, "no-such-ride", "CANCELLED", AS_NUSRAT)
    assert (e.value.code, e.value.status) == ("RIDE_NOT_FOUND", 404)


async def test_every_pool_change_tells_matching_who_and_what(db, bullet):
    _, nusrat, rafiq = bullet
    await drive(db, nusrat, "DRIVER_ARRIVED", "STARTED")
    await transition(db.rw, rafiq, "CANCELLED", AS_JASHIM, "PASSENGER_NO_SHOW")
    for key, data in await events(db):
        if key == "trip.pool.updated":
            assert {"pool_id", "driver_id", "status"} <= data.keys()  # what Matching's consumer reads (4.7)
