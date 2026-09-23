import math

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from tesla_common.errors import DomainError

from .models import Zone, ZoneDistance


def haversine_m(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    r = 6_371_000
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lng2 - lng1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


class DistanceTable:
    ROAD_FACTOR = 1.3

    def __init__(self, zones: dict[str, tuple[float, float]], overrides: dict[tuple[str, str], int]):
        self.zones = zones
        self.overrides = overrides

    def require(self, code: str) -> None:
        if code not in self.zones:
            raise DomainError("UNKNOWN_ZONE", f"Unknown zone {code}", 422)

    def get(self, a: str, b: str) -> int:
        self.require(a)
        self.require(b)
        if a == b:
            return 0
        if (a, b) in self.overrides:
            return self.overrides[(a, b)]
        (la, lo), (lb, lob) = self.zones[a], self.zones[b]
        return int(round(haversine_m(la, lo, lb, lob) * self.ROAD_FACTOR, -2))

    def nearest_zone(self, lat: float, lng: float) -> str:
        return min(self.zones, key=lambda c: haversine_m(lat, lng, *self.zones[c]))


async def load_distance_table(session: AsyncSession) -> DistanceTable:
    """Zones + overrides from matching.db; the lifespan keeps the result in app.state.dist (plan 4.8 step 5)."""
    zones = {z.code: (z.lat, z.lng) for z in (await session.execute(select(Zone))).scalars()}
    overrides = {(d.from_zone, d.to_zone): d.distance_m
                 for d in (await session.execute(select(ZoneDistance))).scalars()}
    return DistanceTable(zones, overrides)
