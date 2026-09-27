from datetime import datetime

from fastapi import APIRouter, Depends, Query, Request, status
from sqlalchemy import select

from tesla_common.auth import Principal
from tesla_common.errors import DomainError

from ..clients import naive_utc
from ..deps import auth, db, fare_client, matching_client
from ..lifecycle import transition
from ..models import Pool, RideRequest, RideStatusHistory
from ..pooling import request_ride
from ..schemas import CancelIn, DriverBrief, RideCreate, RideDetailOut, RideOut, RideStatus

router = APIRouter(prefix="/rides", tags=["passenger"])
passenger = auth.role("PASSENGER")


def _brief(pool: Pool) -> DriverBrief:
    return DriverBrief(driver_id=pool.driver_id, driver_name=pool.driver_name, vehicle_nickname=pool.vehicle_nickname)


async def _ride_out(ride_id: str, user_id: str, detail: bool = False):
    async with db.ro() as s:
        ride = await s.get(RideRequest, ride_id)
        if ride is None or ride.passenger_id != user_id:
            raise DomainError("RIDE_NOT_FOUND", "Ride not found", 404)
        out = RideOut.model_validate(ride)
        if ride.pool_id:
            out.driver = _brief(await s.get(Pool, ride.pool_id))
        if not detail:
            return out
        hist = (await s.execute(select(RideStatusHistory).where(RideStatusHistory.ride_id == ride_id)
                                .order_by(RideStatusHistory.id))).scalars().all()
        return RideDetailOut(**out.model_dump(), history=hist)


@router.post("", response_model=RideOut, status_code=status.HTTP_201_CREATED)
async def create(body: RideCreate, request: Request, p: Principal = Depends(passenger)):
    ride_id = await request_ride(p, body, ro=db.ro, rw=db.rw, fare=fare_client, matching=matching_client,
                                 request_id=request.headers.get("x-request-id"))
    return await _ride_out(ride_id, p.user_id)


@router.get("", response_model=list[RideOut])
async def list_mine(status_: RideStatus | None = Query(default=None, alias="status"),
                    limit: int = Query(default=20, ge=1, le=100), before: datetime | None = None,
                    p: Principal = Depends(passenger)):
    """Newest first. Next page: ?before=<created_at of the last ride on this page>."""
    q = select(RideRequest).where(RideRequest.passenger_id == p.user_id)
    if status_:
        q = q.where(RideRequest.status == status_)
    if before:
        q = q.where(RideRequest.created_at < naive_utc(before))  # a +06:00 time is converted, not relabelled
    async with db.ro() as s:
        rides = (await s.execute(q.order_by(RideRequest.created_at.desc()).limit(limit))).scalars().all()
        pool_ids = {r.pool_id for r in rides if r.pool_id}
        pools = {pl.id: pl for pl in (await s.execute(select(Pool).where(Pool.id.in_(pool_ids)))).scalars()}
    out = []
    for r in rides:
        item = RideOut.model_validate(r)
        if r.pool_id:
            item.driver = _brief(pools[r.pool_id])
        out.append(item)
    return out


@router.get("/{ride_id}", response_model=RideDetailOut)
async def get_one(ride_id: str, p: Principal = Depends(passenger)):
    return await _ride_out(ride_id, p.user_id, detail=True)


@router.post("/{ride_id}/cancel", response_model=RideOut)
async def cancel(ride_id: str, body: CancelIn, p: Principal = Depends(passenger)):
    await transition(db.rw, ride_id, "CANCELLED", p, body.reason)
    return await _ride_out(ride_id, p.user_id)
