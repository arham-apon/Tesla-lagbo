"""5.5: every event Trip emits has exactly the fields of the plan's event registry (0.4). Matching, Fare and
Notification read these; a renamed or missing field would break them silently."""
import json

from sqlalchemy import select

from app.lifecycle import transition
from app.models import Outbox, PoolWaypoint
from app.pooling import request_ride
from app.schemas import RideCreate
from conftest import AS_JASHIM, AS_RAFIQ, AS_SHIRIN, KARIM, FakeFare, FakeMatching, bullet_with_nusrat

REGISTRY = {
    "trip.ride.requested": {"ride_id", "passenger_id", "pickup_zone", "dropoff_zone", "seats",
                            "estimated_fare_poysha", "candidate_driver_ids"},
    "trip.ride.matched": {"ride_id", "pool_id", "passenger_id", "driver_id", "driver_name", "vehicle_nickname",
                          "seats", "joined_existing_pool"},
    "trip.ride.status_changed": {"ride_id", "pool_id", "passenger_id", "driver_id", "from_status", "to_status",
                                 "actor_role"},
    "trip.ride.cancelled": {"ride_id", "pool_id", "passenger_id", "driver_id", "from_status", "cancelled_by",
                            "reason", "quote_id"},
    "trip.ride.completed": {"ride_id", "pool_id", "passenger_id", "driver_id", "quote_id", "seats",
                            "payment_method", "pooled", "co_rider_count"},
    "trip.pool.updated": {"pool_id", "driver_id", "status", "occupied_seats", "max_capacity",
                          "member_passenger_ids", "waypoints"},
}
WAYPOINT = {"seq", "kind", "zone", "ride_id", "done"}


async def envelopes(db) -> list[dict]:
    async with db.ro() as s:
        return [json.loads(r.payload) for r in (await s.execute(select(Outbox).order_by(Outbox.id))).scalars()]


async def a_busy_evening(db):
    """Produces every Trip event type at least once."""
    _, nusrat = await bullet_with_nusrat(db)                                      # matched, pool.updated
    kw = dict(ro=db.ro, rw=db.rw, fare=FakeFare(), request_id=None)
    rafiq = await request_ride(AS_RAFIQ, RideCreate(pickup_zone="BANANI", dropoff_zone="GULSHAN_1", seats=1),
                               matching=FakeMatching(), **kw)                     # joined an existing pool
    await request_ride(AS_SHIRIN, RideCreate(pickup_zone="MIRPUR", dropoff_zone="UTTARA", seats=1),
                       matching=FakeMatching(candidates=[(KARIM, 900)]), **kw)    # requested
    await transition(db.rw, rafiq, "CANCELLED", AS_RAFIQ, "changed_plans")        # cancelled
    for target in ("DRIVER_ARRIVED", "STARTED", "COMPLETED"):                     # status_changed, completed
        await transition(db.rw, nusrat, target, AS_JASHIM)


async def test_every_event_matches_the_registry(db):
    await a_busy_evening(db)
    seen = set()
    for env in await envelopes(db):
        kind = env["event_type"]
        seen.add(kind)
        assert set(env["data"]) == REGISTRY[kind], kind
        if kind == "trip.pool.updated":
            assert all(set(w) == WAYPOINT for w in env["data"]["waypoints"])
    assert seen == set(REGISTRY)


async def test_envelope(db):
    await a_busy_evening(db)
    for env in await envelopes(db):
        assert env["producer"] == "trip-service" and env["version"] == 1
        assert env["occurred_at"].endswith("Z") and len(env["occurred_at"]) == len("2026-09-24T08:41:05.000000Z")


async def test_done_stops_stay_done_when_the_route_changes(db):
    """Nusrat is picked up, then Rafiq cancels: the waypoints are rewritten, but Nusrat's pickup stays done."""
    pool_id, nusrat = await bullet_with_nusrat(db)
    rafiq = await request_ride(AS_RAFIQ, RideCreate(pickup_zone="BANANI", dropoff_zone="GULSHAN_1", seats=1),
                               ro=db.ro, rw=db.rw, fare=FakeFare(), matching=FakeMatching(), request_id=None)
    for target in ("DRIVER_ARRIVED", "STARTED"):
        await transition(db.rw, nusrat, target, AS_JASHIM)
    await transition(db.rw, rafiq, "CANCELLED", AS_JASHIM, "PASSENGER_NO_SHOW")
    async with db.ro() as s:
        rows = (await s.execute(select(PoolWaypoint).where(PoolWaypoint.pool_id == pool_id)
                                .order_by(PoolWaypoint.seq))).scalars().all()
    assert [(w.seq, w.kind, w.ride_request_id, w.done_at is not None) for w in rows] == [
        (1, "PICKUP", nusrat, True), (2, "DROPOFF", nusrat, False)]
    last = [e["data"] for e in await envelopes(db) if e["event_type"] == "trip.pool.updated"][-1]
    assert [(w["seq"], w["done"]) for w in last["waypoints"]] == [(1, True), (2, False)]
