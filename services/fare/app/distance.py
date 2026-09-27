import logging

from redis.asyncio import Redis
from redis.exceptions import RedisError

from tesla_common.errors import DomainError
from tesla_common.http import ServiceClient

log = logging.getLogger("fare.distance")
CACHE_SECONDS = 86400  # 24 h. If a distance override ever changes in Matching, clear fare:dist:* (plan 4.8 note).


async def zone_distance(redis: Redis, matching: ServiceClient, a: str, b: str, request_id: str | None) -> int:
    key = f"fare:dist:{a}:{b}"
    try:
        cached = await redis.get(key)
    except RedisError:
        # The cache only saves a call; a Redis outage must not stop Nusrat getting a price.
        log.warning("distance cache unavailable; asking Matching", exc_info=True)
        cached = None
    if cached is not None:
        return int(cached)
    resp = await matching.request("GET", "/internal/zones/distance", params={"from": a, "to": b},
                                  request_id=request_id, retries=1)
    if resp.status_code == 422:
        raise DomainError("UNKNOWN_ZONE", resp.json()["error"]["message"], 422)
    if resp.status_code != 200:
        # ServiceClient only raises on 5xx; a 401/404 would otherwise crash as a 500 on resp.json()["distance_m"].
        raise DomainError("UPSTREAM_ERROR", f"matching returned {resp.status_code}", 503)
    d = int(resp.json()["distance_m"])
    try:
        await redis.set(key, d, ex=CACHE_SECONDS)
    except RedisError:
        log.warning("distance cache unavailable; not cached", exc_info=True)
    return d
