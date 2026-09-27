import json
from collections.abc import Callable
from datetime import datetime, timedelta, timezone

from redis.asyncio import Redis
from redis.exceptions import WatchError

from .geo import DistanceTable

GEO = "geo:drivers"
AVAILABLE = "drivers:available"
LIVE_POOL_STATUSES = ("FORMING", "IN_PROGRESS")
_EPOCH = datetime(1970, 1, 1)
_MICRO = timedelta(microseconds=1)


def h(driver_id: str) -> str:
    return f"driver:{driver_id}"


async def record_ping(r: Redis, dist: DistanceTable, driver_id: str, lat: float, lng: float, ts: str) -> str:
    zone = dist.nearest_zone(lat, lng)
    async with r.pipeline(transaction=True) as p:
        p.geoadd(GEO, [lng, lat, driver_id])
        p.hset(h(driver_id), mapping={"lat": lat, "lng": lng, "zone": zone, "ping_ts": ts})
        p.set(f"{h(driver_id)}:hb", 1, ex=30)
        p.hget(h(driver_id), "pool_id")
        *_, pool_id = await p.execute()
    if pool_id:
        await r.publish(f"loc:pool:{pool_id}",
                        json.dumps({"pool_id": pool_id, "driver_id": driver_id, "lat": lat, "lng": lng,
                                    "zone": zone, "ts": ts}))
    return zone


def _micros(occurred_at: str) -> int:
    """Event time as integer microseconds. Comparing the ISO strings is wrong: isoformat() drops '.000000',
    so '08:41:05Z' would sort after '08:41:05.120000Z'."""
    dt = datetime.fromisoformat(occurred_at.removesuffix("Z"))
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return (dt - _EPOCH) // _MICRO


Change = Callable[[dict[str, str]], tuple[dict[str, str | int], list[str]]]


async def _apply_if_newer(r: Redis, driver_id: str, ts_field: str, occurred_at: str, change: Change) -> bool:
    """Atomically apply one event to driver:{id}, unless an event at least as new was already applied.

    Events can arrive out of order (the consumer handles up to 20 at once, and retries come back 5 s late),
    so the check and the write happen in one WATCH/MULTI transaction, and "available" is recomputed from
    the resulting state in the same transaction.
    """
    key, ts = h(driver_id), _micros(occurred_at)
    async with r.pipeline(transaction=True) as p:
        while True:
            try:
                await p.watch(key)
                state = await p.hgetall(key)
                if ts_field in state and int(state[ts_field]) >= ts:
                    return False  # stale or replayed
                updates, deletes = change(state)
                updates = {**updates, ts_field: ts}
                after = {**state, **{k: str(v) for k, v in updates.items()}}
                for field in deletes:
                    after.pop(field, None)
                online = after.get("online") == "1"
                p.multi()
                p.hset(key, mapping=updates)
                if deletes:
                    p.hdel(key, *deletes)
                if online and not after.get("pool_id"):
                    p.sadd(AVAILABLE, driver_id)
                else:
                    p.srem(AVAILABLE, driver_id)
                if not online:
                    p.zrem(GEO, driver_id)
                await p.execute()
                return True
            except WatchError:
                continue  # a ping or another event changed the hash meanwhile; re-read and retry


async def on_driver_online(r: Redis, driver_id: str, occurred_at: str) -> bool:
    return await _apply_if_newer(r, driver_id, "state_ts", occurred_at, lambda s: ({"online": 1}, []))


async def on_driver_offline(r: Redis, driver_id: str, occurred_at: str) -> bool:
    return await _apply_if_newer(r, driver_id, "state_ts", occurred_at, lambda s: ({"online": 0}, []))


async def on_pool_updated(r: Redis, driver_id: str, pool_id: str, status: str, occurred_at: str) -> bool:
    def change(state: dict[str, str]):
        if status in LIVE_POOL_STATUSES:
            return {"pool_id": pool_id}, []
        # COMPLETED / CANCELLED: free him, but only if this is still his current pool.
        return {}, (["pool_id"] if state.get("pool_id") == pool_id else [])
    return await _apply_if_newer(r, driver_id, "pool_ts", occurred_at, change)


async def nearby_available(r: Redis, lat: float, lng: float, radius_m: int, limit: int) -> list[tuple[str, int]]:
    hits = await r.geosearch(GEO, longitude=lng, latitude=lat, radius=radius_m, unit="m",
                             sort="ASC", count=limit * 4, withdist=True)
    if not hits:
        return []
    async with r.pipeline(transaction=False) as p:
        for member, _ in hits:
            p.sismember(AVAILABLE, member)
            p.exists(f"{h(member)}:hb")
        flags = await p.execute()
    out = []
    for i, (member, d) in enumerate(hits):
        if flags[2 * i] and flags[2 * i + 1]:
            out.append((member, int(d)))
        if len(out) == limit:
            break
    return out
