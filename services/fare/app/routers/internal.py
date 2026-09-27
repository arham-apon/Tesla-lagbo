from fastapi import APIRouter, Depends, Request, status

from ..deps import auth, db, matching_http, redis, settings
from ..quotes import create_quote, get_quote
from ..schemas import InternalQuoteIn, QuoteOut

# Other services only (Trip): every route needs the internal token.
router = APIRouter(prefix="/internal", tags=["internal"], dependencies=[Depends(auth.require_internal())])


@router.post("/quotes", response_model=QuoteOut, status_code=status.HTTP_201_CREATED)
async def new_quote(body: InternalQuoteIn, request: Request):
    return await create_quote(db.rw, redis, matching_http, body.passenger_id, body.pickup_zone, body.dropoff_zone,
                              body.seats, settings.QUOTE_TTL_SECONDS, request.headers.get("x-request-id"))


@router.get("/quotes/{quote_id}", response_model=QuoteOut)
async def quote(quote_id: str):
    return await get_quote(db.ro, quote_id)
