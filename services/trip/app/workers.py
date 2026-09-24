import asyncio
import logging
from datetime import timedelta

from sqlalchemy import select

from tesla_common.errors import DomainError
from tesla_common.timeutil import utcnow

from .lifecycle import transition
from .models import RideRequest

log = logging.getLogger("trip.sweeper")


async def expire_stale_requests(ro, rw, ttl_seconds: int, stop: asyncio.Event, every: float = 15.0) -> None:
    while not stop.is_set():
        try:
            cutoff = utcnow() - timedelta(seconds=ttl_seconds)
            async with ro() as s:
                ids = (await s.execute(select(RideRequest.id).where(
                    RideRequest.status == "REQUESTED", RideRequest.created_at < cutoff).limit(100))).scalars().all()
            for ride_id in ids:
                try:
                    await transition(rw, ride_id, "CANCELLED", None, "NO_DRIVER_FOUND")
                except DomainError:
                    pass
        except Exception:
            log.exception("sweeper iteration failed")
        await asyncio.sleep(every)
