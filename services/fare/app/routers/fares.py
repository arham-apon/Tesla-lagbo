from fastapi import APIRouter, Depends, Request, status
from sqlalchemy import select

from tesla_common.auth import Principal
from tesla_common.errors import DomainError

from ..deps import auth, db, matching_http, redis, settings
from ..models import Fare
from ..quotes import create_quote
from ..schemas import EstimateIn, FareOut, QuoteOut

router = APIRouter(prefix="/fares", tags=["fares"])


@router.post("/estimate", response_model=QuoteOut, status_code=status.HTTP_201_CREATED)
async def estimate(body: EstimateIn, request: Request, p: Principal = Depends(auth.role("PASSENGER"))):
    """Solo and pooled price for this route, valid 10 minutes. The app passes quote_id on to POST /rides."""
    return await create_quote(db.rw, redis, matching_http, p.user_id, body.pickup_zone, body.dropoff_zone,
                              body.seats, settings.QUOTE_TTL_SECONDS, request.headers.get("x-request-id"))


@router.get("/rides/{ride_id}", response_model=FareOut)
async def ride_fare(ride_id: str, p: Principal = Depends(auth.role("PASSENGER", "DRIVER"))):
    """Nusrat sees the fare of her own ride; Jashim sees the fare of a ride he drove. Anyone else: 404."""
    async with db.ro() as s:
        fare = await s.scalar(select(Fare).where(Fare.ride_id == ride_id))
    mine = fare is not None and (fare.passenger_id if p.role == "PASSENGER" else fare.driver_id) == p.user_id
    if not mine:
        raise DomainError("FARE_NOT_FOUND", "Fare not found", 404)
    return fare
