from functools import partial

from redis.asyncio import Redis

from . import fleet

QUEUE = "matching.fleet-state"
BINDINGS = ["identity.driver.*", "trip.pool.updated"]


async def handle(redis: Redis, env: dict) -> None:
    data, kind, ts = env["data"], env["event_type"], env["occurred_at"]
    if kind == "identity.driver.online":
        await fleet.on_driver_online(redis, data["driver_id"], ts)
    elif kind == "identity.driver.offline":
        await fleet.on_driver_offline(redis, data["driver_id"], ts)
    elif kind == "trip.pool.updated":
        # ts orders pool events per driver (4.5): a stale FORMING back from the retry queue is ignored.
        await fleet.on_pool_updated(redis, data["driver_id"], data["pool_id"], data["status"], ts)


async def start(bus, redis: Redis) -> None:
    await bus.consume(QUEUE, BINDINGS, partial(handle, redis))
