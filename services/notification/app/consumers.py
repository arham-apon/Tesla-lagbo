import json
from functools import partial

from tesla_common.events import first_time

from .connections import ConnectionManager
from .models import Notification, ProcessedEvent
from .routing import recipients

BINDINGS = ["trip.ride.*", "trip.pool.updated", "fare.ride.settled"]
MEMBERS_TTL_SECONDS = 6 * 3600  # a pool's passenger list outlives any real ride, then cleans itself up
LIVE = ("FORMING", "IN_PROGRESS")


def members_key(pool_id: str) -> str:
    return f"notif:pool:{pool_id}:members"


async def _replace_members(redis, d: dict) -> None:
    """Who may see the car move (7.6's location relay reads this). A full replacement, so repeating it is harmless."""
    key = members_key(d["pool_id"])
    async with redis.pipeline(transaction=True) as p:
        p.delete(key)
        if d["member_passenger_ids"] and d["status"] in LIVE:
            p.sadd(key, *d["member_passenger_ids"])
            p.expire(key, MEMBERS_TTL_SECONDS)
        await p.execute()


async def persist(rw, redis, env: dict) -> None:
    """notification.inbox: durable (retries, then the DLQ). The inbox is the record of what was sent."""
    async with rw.begin() as s:
        if not await first_time(s, ProcessedEvent, env["event_id"]):
            return
        for user_id, message in recipients(env):
            if message["type"] != "pool.updated":  # frequent, for the driver only, and on Trip's /driver/pool anyway
                s.add(Notification(user_id=user_id, type=message["type"], payload=json.dumps(message)))
        if env["event_type"] == "trip.pool.updated":
            # Change from the plan (finding 3): the plan wrote this AFTER the commit, so a Redis error left the
            # event marked "processed" with no member list, and the retry skipped it. Here it's written before the
            # commit: a Redis error rolls the whole thing back, and the retry really runs.
            await _replace_members(redis, env["data"])


async def push(manager: ConnectionManager, env: dict) -> None:
    """The per-copy broadcast queue: best-effort, to the sockets this copy holds. Missed? The inbox has it."""
    for user_id, message in recipients(env):
        await manager.send(user_id, message)


async def start(bus, rw, redis, manager: ConnectionManager) -> None:
    await bus.consume("notification.inbox", BINDINGS, partial(persist, rw, redis))
    await bus.consume_broadcast(BINDINGS, partial(push, manager))
