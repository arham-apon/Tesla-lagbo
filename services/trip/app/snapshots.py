from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .models import ACTIVE_RIDE_STATUSES, Pool, PoolWaypoint, RideRequest


async def pool_view(s: AsyncSession, pool: Pool) -> dict:
    rides = (await s.execute(select(RideRequest).where(RideRequest.pool_id == pool.id))).scalars().all()
    names = {r.id: r.passenger_name for r in rides}
    wps = (await s.execute(select(PoolWaypoint).where(PoolWaypoint.pool_id == pool.id)
                           .order_by(PoolWaypoint.seq))).scalars().all()
    return {
        "id": pool.id, "status": pool.status, "vehicle_nickname": pool.vehicle_nickname,
        "occupied_seats": pool.occupied_seats, "max_capacity": pool.max_capacity,
        "riders": [{"ride_id": r.id, "passenger_name": r.passenger_name, "seats": r.seats, "status": r.status,
                    "pickup_zone": r.pickup_zone, "dropoff_zone": r.dropoff_zone} for r in rides],
        "waypoints": [{"seq": w.seq, "kind": w.kind, "zone": w.zone, "ride_id": w.ride_request_id,
                       "passenger_name": names.get(w.ride_request_id, ""), "done": w.done_at is not None}
                      for w in wps],
        "_member_ids": [r.passenger_id for r in rides if r.status in ACTIVE_RIDE_STATUSES],
    }


def pool_updated_payload(pool: Pool, view: dict) -> dict:
    return {
        "pool_id": pool.id, "driver_id": pool.driver_id, "status": pool.status,
        "occupied_seats": pool.occupied_seats, "max_capacity": pool.max_capacity,
        "member_passenger_ids": view["_member_ids"],
        "waypoints": [{k: w[k] for k in ("seq", "kind", "zone", "ride_id", "done")} for w in view["waypoints"]],
    }


async def replace_waypoints(s: AsyncSession, pool_id: str, stops: list[dict]) -> None:
    existing = (await s.execute(select(PoolWaypoint).where(PoolWaypoint.pool_id == pool_id))).scalars().all()
    done = {(w.ride_request_id, w.kind): w.done_at for w in existing}
    for w in existing:
        await s.delete(w)
    await s.flush()
    for seq, st in enumerate(stops, start=1):
        s.add(PoolWaypoint(pool_id=pool_id, ride_request_id=st["ride_id"], seq=seq, kind=st["kind"],
                           zone=st["zone"], done_at=done.get((st["ride_id"], st["kind"]))))
