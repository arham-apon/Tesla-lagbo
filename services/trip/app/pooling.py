import enum

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tesla_common.auth import Principal
from tesla_common.errors import DomainError
from tesla_common.events import emit
from tesla_common.timeutil import new_id, utcnow

from .clients import FareClient, MatchingClient
from .models import (DriverShift, Outbox, Pool, PoolWaypoint, RideOffer, RideRequest, RideStatusHistory)
from .schemas import RideCreate
from .snapshots import pool_updated_payload, pool_view, replace_waypoints

PRODUCER = "trip-service"
NEW_RIDE = "__new__"


class JoinResult(enum.Enum):
    JOINED = "joined"
    STALE = "stale"
    NONE = "none"


def history(s: AsyncSession, ride_id: str, frm: str | None, to: str, actor_id: str, role: str, reason=None):
    s.add(RideStatusHistory(ride_id=ride_id, from_status=frm, to_status=to, actor_id=actor_id,
                            actor_role=role, reason=reason))


async def open_pool_snapshot(ro: async_sessionmaker, pickup_zone: str, seats: int) -> list[dict]:
    async with ro() as s:
        pools = (await s.execute(
            select(Pool).where(Pool.status == "FORMING", Pool.pickup_zone == pickup_zone,
                               Pool.max_capacity - Pool.occupied_seats >= seats)
        )).scalars().all()
        out = []
        for p in pools:
            wps = (await s.execute(select(PoolWaypoint).where(PoolWaypoint.pool_id == p.id)
                                   .order_by(PoolWaypoint.seq))).scalars().all()
            out.append({"pool_id": p.id, "driver_id": p.driver_id, "pickup_zone": p.pickup_zone,
                        "remaining_seats": p.max_capacity - p.occupied_seats, "version": p.version,
                        "stops": [{"ride_id": w.ride_request_id, "kind": w.kind, "zone": w.zone,
                                   "done": w.done_at is not None} for w in wps]})
        return out


async def try_join(rw: async_sessionmaker, ride_id: str, seats: int, options: list[dict]) -> JoinResult:
    if not options:
        return JoinResult.NONE
    async with rw.begin() as s:
        for opt in options:
            res = await s.execute(
                update(Pool)
                .where(Pool.id == opt["pool_id"], Pool.status == "FORMING", Pool.version == opt["version"],
                       Pool.occupied_seats + seats <= Pool.max_capacity)
                .values(occupied_seats=Pool.occupied_seats + seats, version=Pool.version + 1)
            )
            if res.rowcount != 1:
                continue
            ride_res = await s.execute(
                update(RideRequest).where(RideRequest.id == ride_id, RideRequest.status == "REQUESTED")
                .values(status="MATCHED", pool_id=opt["pool_id"], version=RideRequest.version + 1,
                        updated_at=utcnow())
            )
            if ride_res.rowcount != 1:
                raise DomainError("RIDE_NO_LONGER_REQUESTED", "Ride was cancelled during matching", 409)
            stops = [{**st, "ride_id": ride_id if st["ride_id"] == NEW_RIDE else st["ride_id"]} for st in opt["plan"]]
            await replace_waypoints(s, opt["pool_id"], stops)
            history(s, ride_id, "REQUESTED", "MATCHED", "system", "SYSTEM", "auto_joined_pool")
            await s.flush()
            pool = await s.get(Pool, opt["pool_id"])
            await s.refresh(pool)
            ride = await s.get(RideRequest, ride_id)
            await s.refresh(ride)
            view = await pool_view(s, pool)
            emit(s, Outbox, PRODUCER, "trip.ride.matched", {
                "ride_id": ride_id, "pool_id": pool.id, "passenger_id": ride.passenger_id,
                "driver_id": pool.driver_id, "driver_name": pool.driver_name,
                "vehicle_nickname": pool.vehicle_nickname, "seats": seats, "joined_existing_pool": True})
            emit(s, Outbox, PRODUCER, "trip.pool.updated", pool_updated_payload(pool, view))
            return JoinResult.JOINED
    return JoinResult.STALE


async def request_ride(p: Principal, body: RideCreate, *, ro, rw, fare: FareClient, matching: MatchingClient,
                       request_id: str | None) -> str:
    quote = await fare.quote_for(p.user_id, body.pickup_zone, body.dropoff_zone, body.seats, body.quote_id, request_id)
    ride_id = new_id()
    try:
        async with rw.begin() as s:
            s.add(RideRequest(id=ride_id, passenger_id=p.user_id, passenger_name=p.name, seats=body.seats,
                              pickup_zone=body.pickup_zone, dropoff_zone=body.dropoff_zone,
                              payment_method=body.payment_method, quote_id=quote.quote_id,
                              solo_distance_m=quote.distance_m, estimated_fare_poysha=quote.solo_total_poysha,
                              estimated_pooled_fare_poysha=quote.pooled_total_poysha))
            await s.flush()
            history(s, ride_id, None, "REQUESTED", p.user_id, "PASSENGER")
    except IntegrityError:
        raise DomainError("ACTIVE_RIDE_EXISTS", "You already have an active ride", 409)

    evaluation: dict = {"candidate_drivers": []}
    for _ in range(3):
        snapshot = await open_pool_snapshot(ro, body.pickup_zone, body.seats)
        evaluation = await matching.evaluate({"pickup_zone": body.pickup_zone, "dropoff_zone": body.dropoff_zone,
                                              "seats": body.seats, "open_pools": snapshot}, request_id)
        result = await try_join(rw, ride_id, body.seats, evaluation["compatible_pools"])
        if result is JoinResult.JOINED:
            return ride_id
        if result is JoinResult.NONE:
            break

    async with rw.begin() as s:
        candidates = evaluation["candidate_drivers"]
        for c in candidates:
            s.add(RideOffer(ride_id=ride_id, driver_id=c["driver_id"], distance_m=c["distance_m"]))
        emit(s, Outbox, PRODUCER, "trip.ride.requested", {
            "ride_id": ride_id, "passenger_id": p.user_id, "pickup_zone": body.pickup_zone,
            "dropoff_zone": body.dropoff_zone, "seats": body.seats,
            "estimated_fare_poysha": quote.solo_total_poysha,
            "candidate_driver_ids": [c["driver_id"] for c in candidates]})
    return ride_id


async def accept_offer(rw: async_sessionmaker, driver: Principal, ride_id: str) -> str:
    async with rw.begin() as s:
        offer = await s.get(RideOffer, (ride_id, driver.user_id))
        if offer is None or offer.status != "OFFERED":
            raise DomainError("OFFER_NOT_FOUND", "No open offer for this ride", 404)
        shift = await s.get(DriverShift, driver.user_id)
        if shift is None or not shift.is_online:
            raise DomainError("DRIVER_OFFLINE", "Go online before accepting rides", 409)
        ride = await s.get(RideRequest, ride_id)
        if ride.seats > shift.seat_capacity:
            raise DomainError("VEHICLE_TOO_SMALL", f"{shift.vehicle_nickname} has {shift.seat_capacity} seats", 409)
        pool = Pool(driver_id=shift.driver_id, vehicle_id=shift.vehicle_id, vehicle_nickname=shift.vehicle_nickname,
                    driver_name=shift.driver_name, max_capacity=shift.seat_capacity, occupied_seats=ride.seats,
                    pickup_zone=ride.pickup_zone)
        s.add(pool)
        try:
            await s.flush()
        except IntegrityError:
            raise DomainError("DRIVER_HAS_LIVE_POOL", "You already have a live pool; new riders join it automatically", 409)
        res = await s.execute(
            update(RideRequest).where(RideRequest.id == ride_id, RideRequest.status == "REQUESTED")
            .values(status="MATCHED", pool_id=pool.id, version=RideRequest.version + 1, updated_at=utcnow()))
        if res.rowcount != 1:
            raise DomainError("RIDE_NO_LONGER_AVAILABLE", "Ride was taken or cancelled", 409)
        offer.status = "ACCEPTED"
        await replace_waypoints(s, pool.id, [
            {"ride_id": ride_id, "kind": "PICKUP", "zone": ride.pickup_zone},
            {"ride_id": ride_id, "kind": "DROPOFF", "zone": ride.dropoff_zone}])
        history(s, ride_id, "REQUESTED", "MATCHED", driver.user_id, "DRIVER", "driver_accepted")
        await s.flush()
        view = await pool_view(s, pool)
        emit(s, Outbox, PRODUCER, "trip.ride.matched", {
            "ride_id": ride_id, "pool_id": pool.id, "passenger_id": ride.passenger_id, "driver_id": pool.driver_id,
            "driver_name": pool.driver_name, "vehicle_nickname": pool.vehicle_nickname, "seats": ride.seats,
            "joined_existing_pool": False})
        emit(s, Outbox, PRODUCER, "trip.pool.updated", pool_updated_payload(pool, view))
        return pool.id
