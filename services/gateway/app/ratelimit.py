import time

from redis.asyncio import Redis

from tesla_common.errors import DomainError


async def enforce(redis: Redis, key_id: str, limit: int, window: int = 60) -> None:
    bucket = int(time.time() // window)
    key = f"rl:{key_id}:{bucket}"
    async with redis.pipeline(transaction=True) as p:
        p.incr(key)
        p.expire(key, window)
        count, _ = await p.execute()
    if count > limit:
        raise DomainError("RATE_LIMITED", f"Limit {limit}/min exceeded", 429)
