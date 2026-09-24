"""Making and reading quotes. Shared by the passenger's estimate (routers/fares.py) and Trip's internal
quote (routers/internal.py): the plan says the two are "the same"."""
from datetime import timedelta

from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from tesla_common.errors import DomainError
from tesla_common.http import ServiceClient
from tesla_common.timeutil import utcnow

from .distance import zone_distance
from .models import Quote, Tariff
from .pricing import Breakdown, compute
from .schemas import BreakdownOut, QuoteOut


def quote_out(q: Quote, t: Tariff) -> QuoteOut:
    """The breakdowns are recomputed from the quote's own tariff and distance, so they always match its totals."""
    solo, pooled = compute(t, q.distance_m, q.seats, False), compute(t, q.distance_m, q.seats, True)
    return QuoteOut(quote_id=q.id, passenger_id=q.passenger_id, pickup_zone=q.pickup_zone,
                    dropoff_zone=q.dropoff_zone, seats=q.seats, distance_m=q.distance_m,
                    solo_total_poysha=q.solo_total_poysha, pooled_total_poysha=q.pooled_total_poysha,
                    solo=BreakdownOut(**vars(solo)), pooled=BreakdownOut(**vars(pooled)), expires_at=q.expires_at)


async def create_quote(rw: async_sessionmaker, redis: Redis, matching: ServiceClient, passenger_id: str,
                       pickup: str, dropoff: str, seats: int, ttl_seconds: int, request_id: str | None) -> QuoteOut:
    distance_m = await zone_distance(redis, matching, pickup, dropoff, request_id)  # before the write lock
    async with rw.begin() as s:
        tariff = await s.scalar(select(Tariff).where(Tariff.active.is_(True)))
        if tariff is None:
            raise DomainError("NO_ACTIVE_TARIFF", "Pricing is not configured", 503)
        solo: Breakdown = compute(tariff, distance_m, seats, pooled=False)
        pooled: Breakdown = compute(tariff, distance_m, seats, pooled=True)
        q = Quote(passenger_id=passenger_id, pickup_zone=pickup, dropoff_zone=dropoff, seats=seats,
                  distance_m=distance_m, tariff_id=tariff.id, solo_total_poysha=solo.total_poysha,
                  pooled_total_poysha=pooled.total_poysha, expires_at=utcnow() + timedelta(seconds=ttl_seconds))
        s.add(q)
        await s.flush()
        return quote_out(q, tariff)


async def get_quote(ro: async_sessionmaker, quote_id: str) -> QuoteOut:
    async with ro() as s:
        q = await s.get(Quote, quote_id)
        # A voided quote (its ride was cancelled, 6.6) can't book another ride (finding 2 in 6.2).
        if q is None or q.voided:
            raise DomainError("QUOTE_NOT_FOUND", "Quote not found", 404)
        return quote_out(q, await s.get(Tariff, q.tariff_id))
