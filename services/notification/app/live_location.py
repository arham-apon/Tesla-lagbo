"""Redis Pub/Sub -> sockets: Jashim's GPS (published by Matching on loc:pool:{pool_id}, 4.5) to his pool's passengers.

Changes from the plan: one bad message no longer ends the relay (it's skipped), and a Redis error doesn't end it
either (it re-subscribes after a short pause). In the plan, either one stopped live locations until a restart.
"""
import asyncio
import json
import logging

from .consumers import members_key

log = logging.getLogger("notification.live")
FIELDS = ("lat", "lng", "zone", "ts")  # never the driver id: passengers see where the car is, nothing more
RETRY_SECONDS = 1.0


async def _forward(redis, manager, raw) -> None:
    data = json.loads(raw)
    members = await redis.smembers(members_key(data["pool_id"]))
    payload = {"type": "vehicle.location", "data": {k: data[k] for k in FIELDS}}
    for user_id in members:
        await manager.send(user_id, payload)


async def relay_locations(redis, manager, stop: asyncio.Event) -> None:
    while not stop.is_set():
        pubsub = redis.pubsub()
        try:
            await pubsub.psubscribe("loc:pool:*")
            while not stop.is_set():
                msg = await pubsub.get_message(ignore_subscribe_messages=True, timeout=1.0)
                if msg is None or msg["type"] != "pmessage":
                    continue
                try:
                    await _forward(redis, manager, msg["data"])
                except (ValueError, KeyError, TypeError):
                    log.warning("skipped a bad location message on %s", msg.get("channel"))
        except Exception:
            log.exception("location relay lost Redis; re-subscribing")
            await asyncio.sleep(RETRY_SECONDS)
        finally:
            try:
                await pubsub.punsubscribe("loc:pool:*")
                await pubsub.aclose()
            except Exception:
                pass  # Redis already gone; nothing to tidy
