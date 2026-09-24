from fastapi import APIRouter, Depends, Query, Response, status
from sqlalchemy import select, update

from tesla_common.auth import Principal
from tesla_common.errors import DomainError

from ..deps import auth, db
from ..lifecycle import transition
from ..models import LIVE_POOL_STATUSES, Pool, RideOffer, RideRequest
from ..pooling import accept_offer
from ..schemas import DriverCancelIn, OfferOut, PoolOut
from ..snapshots import pool_view

router = APIRouter(prefix="/driver", tags=["driver"])
driver = auth.role("DRIVER")


async def _pool_out(pool_id: str) -> PoolOut:
    async with db.ro() as s:
        return PoolOut(**await pool_view(s, await s.get(Pool, pool_id)))


# ---- offers -----------------------------------------------------------------------------------------------------

@router.get("/offers", response_model=list[OfferOut])
async def offers(p: Principal = Depends(driver)):
    """Open offers for rides nobody has taken yet, oldest first (the rider who has waited longest)."""
    async with db.ro() as s:
        rows = (await s.execute(
            select(RideOffer, RideRequest).join(RideRequest, RideRequest.id == RideOffer.ride_id)
            .where(RideOffer.driver_id == p.user_id, RideOffer.status == "OFFERED",
                   RideRequest.status == "REQUESTED")
            .order_by(RideOffer.created_at))).all()
    return [OfferOut(ride_id=r.id, passenger_name=r.passenger_name, pickup_zone=r.pickup_zone,
                     dropoff_zone=r.dropoff_zone, seats=r.seats, estimated_fare_poysha=r.estimated_fare_poysha,
                     distance_m=o.distance_m, offered_at=o.created_at) for o, r in rows]


@router.post("/offers/{ride_id}/accept", response_model=PoolOut)
async def accept(ride_id: str, p: Principal = Depends(driver)):
    return await _pool_out(await accept_offer(db.rw, p, ride_id))


@router.post("/offers/{ride_id}/decline", status_code=status.HTTP_204_NO_CONTENT)
async def decline(ride_id: str, p: Principal = Depends(driver)):
    async with db.rw.begin() as s:
        res = await s.execute(update(RideOffer).where(RideOffer.ride_id == ride_id, RideOffer.driver_id == p.user_id,
                                                      RideOffer.status == "OFFERED").values(status="DECLINED"))
        if res.rowcount != 1:
            raise DomainError("OFFER_NOT_FOUND", "No open offer for this ride", 404)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# ---- pools ------------------------------------------------------------------------------------------------------

@router.get("/pool", response_model=PoolOut, responses={204: {"description": "No live pool"}})
async def live_pool(p: Principal = Depends(driver)):
    async with db.ro() as s:
        pool = (await s.execute(select(Pool).where(Pool.driver_id == p.user_id,
                                                   Pool.status.in_(LIVE_POOL_STATUSES)))).scalar_one_or_none()
        if pool is None:
            return Response(status_code=status.HTTP_204_NO_CONTENT)
        return PoolOut(**await pool_view(s, pool))


@router.get("/pools", response_model=list[PoolOut])
async def past_pools(limit: int = Query(default=20, ge=1, le=100), p: Principal = Depends(driver)):
    async with db.ro() as s:
        pools = (await s.execute(select(Pool).where(Pool.driver_id == p.user_id,
                                                    Pool.status.not_in(LIVE_POOL_STATUSES))
                                 .order_by(Pool.created_at.desc()).limit(limit))).scalars().all()
        return [PoolOut(**await pool_view(s, pool)) for pool in pools]


# ---- moving a rider through the trip ----------------------------------------------------------------------------

async def _move(ride_id: str, target: str, p: Principal, reason: str | None = None) -> PoolOut:
    await transition(db.rw, ride_id, target, p, reason)  # ownership (his pool only) + state machine inside
    async with db.ro() as s:
        pool_id = (await s.get(RideRequest, ride_id)).pool_id
    return await _pool_out(pool_id)


@router.post("/rides/{ride_id}/arrive", response_model=PoolOut)
async def arrive(ride_id: str, p: Principal = Depends(driver)):
    return await _move(ride_id, "DRIVER_ARRIVED", p)


@router.post("/rides/{ride_id}/start", response_model=PoolOut)
async def start(ride_id: str, p: Principal = Depends(driver)):
    return await _move(ride_id, "STARTED", p)


@router.post("/rides/{ride_id}/complete", response_model=PoolOut)
async def complete(ride_id: str, p: Principal = Depends(driver)):
    return await _move(ride_id, "COMPLETED", p)


@router.post("/rides/{ride_id}/cancel", response_model=PoolOut)
async def cancel(ride_id: str, body: DriverCancelIn, p: Principal = Depends(driver)):
    return await _move(ride_id, "CANCELLED", p, body.reason)
