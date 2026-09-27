from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tesla_common.auth import Principal
from tesla_common.errors import DomainError
from tesla_common.events import emit
from tesla_common.timeutil import utcnow

from .models import ACTIVE_RIDE_STATUSES, Outbox, Pool, PoolWaypoint, RideRequest
from .pooling import PRODUCER, history
from .snapshots import pool_updated_payload, pool_view, replace_waypoints
from .state_machine import Actor, assert_transition


def _actor(p: Principal | None) -> Actor:
    return "SYSTEM" if p is None else ("DRIVER" if p.role == "DRIVER" else "PASSENGER")


async def _load_owned(s: AsyncSession, ride_id: str, p: Principal | None) -> tuple[RideRequest, Pool | None]:
    ride = await s.get(RideRequest, ride_id)
    pool = await s.get(Pool, ride.pool_id) if ride and ride.pool_id else None
    if ride is None:
        raise DomainError("RIDE_NOT_FOUND", "Ride not found", 404)
    if p is not None:
        owns = ride.passenger_id == p.user_id if p.role == "PASSENGER" else (pool is not None and pool.driver_id == p.user_id)
        if not owns:
            raise DomainError("RIDE_NOT_FOUND", "Ride not found", 404)
    return ride, pool


async def _release_seats(s: AsyncSession, pool: Pool, ride: RideRequest, cancelled: bool) -> None:
    await s.execute(update(Pool).where(Pool.id == pool.id)
                    .values(occupied_seats=Pool.occupied_seats - ride.seats, version=Pool.version + 1))
    wps = (await s.execute(select(PoolWaypoint).where(PoolWaypoint.pool_id == pool.id)
                           .order_by(PoolWaypoint.seq))).scalars().all()
    if cancelled:
        stops = [{"ride_id": w.ride_request_id, "kind": w.kind, "zone": w.zone}
                 for w in wps if w.ride_request_id != ride.id]
        await replace_waypoints(s, pool.id, stops)
    else:
        for w in wps:
            if w.ride_request_id == ride.id and w.kind == "DROPOFF":
                w.done_at = utcnow()
    await s.flush()
    await s.refresh(pool)
    active = await s.scalar(select(func.count()).select_from(RideRequest)
                            .where(RideRequest.pool_id == pool.id, RideRequest.status.in_(ACTIVE_RIDE_STATUSES)))
    if active == 0:
        completed = await s.scalar(select(func.count()).select_from(RideRequest)
                                   .where(RideRequest.pool_id == pool.id, RideRequest.status == "COMPLETED"))
        pool.status = "COMPLETED" if completed else "CANCELLED"
        pool.completed_at = utcnow()


async def transition(rw: async_sessionmaker, ride_id: str, target: str, p: Principal | None,
                     reason: str | None = None) -> None:
    async with rw.begin() as s:
        ride, pool = await _load_owned(s, ride_id, p)
        actor = _actor(p)
        assert_transition(ride.status, target, actor)
        frm = ride.status
        res = await s.execute(
            update(RideRequest).where(RideRequest.id == ride.id, RideRequest.version == ride.version)
            .values(status=target, version=RideRequest.version + 1, updated_at=utcnow(),
                    cancel_reason=reason if target == "CANCELLED" else RideRequest.cancel_reason))
        if res.rowcount != 1:
            raise DomainError("CONCURRENT_MODIFICATION", "Ride changed; retry", 409)
        await s.refresh(ride)
        history(s, ride.id, frm, target, p.user_id if p else "system", actor, reason)

        if pool is not None:
            if target == "STARTED":
                await s.execute(update(PoolWaypoint).where(PoolWaypoint.pool_id == pool.id,
                                                           PoolWaypoint.ride_request_id == ride.id,
                                                           PoolWaypoint.kind == "PICKUP").values(done_at=utcnow()))
                if pool.status == "FORMING":
                    pool.status, pool.started_at = "IN_PROGRESS", utcnow()
            elif target in ("CANCELLED", "COMPLETED"):
                await _release_seats(s, pool, ride, cancelled=target == "CANCELLED")

        base = {"ride_id": ride.id, "pool_id": pool.id if pool else None, "passenger_id": ride.passenger_id,
                "driver_id": pool.driver_id if pool else None}
        if target == "CANCELLED":
            emit(s, Outbox, PRODUCER, "trip.ride.cancelled", base | {
                "from_status": frm, "cancelled_by": actor, "reason": reason, "quote_id": ride.quote_id})
        elif target == "COMPLETED":
            riders = await s.scalar(select(func.count()).select_from(RideRequest)
                                    .where(RideRequest.pool_id == pool.id, RideRequest.status != "CANCELLED"))
            emit(s, Outbox, PRODUCER, "trip.ride.completed", base | {
                "quote_id": ride.quote_id, "seats": ride.seats, "payment_method": ride.payment_method,
                "pooled": riders >= 2, "co_rider_count": riders - 1})
        else:
            emit(s, Outbox, PRODUCER, "trip.ride.status_changed", base | {
                "from_status": frm, "to_status": target, "actor_role": actor})
        if pool is not None:
            await s.flush()
            view = await pool_view(s, pool)
            emit(s, Outbox, PRODUCER, "trip.pool.updated", pool_updated_payload(pool, view))
