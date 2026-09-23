"""4.5 / 4.8.4: driver state in Redis: pings, heartbeat, availability from events (incl. out-of-order), nearby search."""
import asyncio
import json
import random
from datetime import datetime, timedelta

import pytest

from app import fleet
from app.fleet import AVAILABLE, GEO, h

JASHIM, KARIM = "jashim", "karim"
T0 = datetime(2026, 9, 24, 8, 41, 5)  # a whole second: isoformat() gives "...05" with no fraction


def at(ms: int = 0) -> str:
    """occurred_at exactly as tesla_common.events.emit writes it."""
    return (T0 + timedelta(milliseconds=ms)).isoformat() + "Z"


async def available(r) -> set[str]:
    return await r.smembers(AVAILABLE)


async def on_map(r, driver: str) -> bool:
    return (await r.geopos(GEO, driver))[0] is not None


async def next_message(sub, timeout: float):
    """First real Pub/Sub message within `timeout`, else None (get_message returns None for confirmations)."""
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        msg = await sub.get_message(ignore_subscribe_messages=True, timeout=0.05)
        if msg is not None:
            return msg
    return None


# ---- timestamps -------------------------------------------------------------------------------------------------

def test_whole_second_sorts_before_later_fraction():
    assert at(0) == "2026-09-24T08:41:05Z" and at(120) == "2026-09-24T08:41:05.120000Z"
    assert at(120) < at(0)                                 # string order: WRONG (why the plan's check was risky)
    assert fleet._micros(at(120)) > fleet._micros(at(0))   # real time order: right


def test_timezones_compare_as_the_same_instant():
    assert fleet._micros("2026-09-24T08:41:05+00:00") == fleet._micros("2026-09-24T14:41:05+06:00") == \
           fleet._micros("2026-09-24T08:41:05Z")


# ---- online / offline -------------------------------------------------------------------------------------------

async def test_online_makes_available(redis):
    assert await fleet.on_driver_online(redis, JASHIM, at(0))
    assert await available(redis) == {JASHIM}
    assert await redis.hget(h(JASHIM), "online") == "1"


async def test_offline_removes_from_available_and_map(redis, dist):
    await fleet.on_driver_online(redis, JASHIM, at(0))
    await fleet.record_ping(redis, dist, JASHIM, 23.7937, 90.4066, at(10))
    await fleet.on_driver_offline(redis, JASHIM, at(20))
    assert await available(redis) == set()
    assert not await on_map(redis, JASHIM)


async def test_replayed_event_is_ignored(redis):
    assert await fleet.on_driver_online(redis, JASHIM, at(0))
    assert not await fleet.on_driver_online(redis, JASHIM, at(0))


async def test_offline_on_the_second_then_online_120ms_later(redis):
    # The case from 4.1: with string comparison, the later "online" looked older and was dropped.
    await fleet.on_driver_offline(redis, JASHIM, at(0))
    assert await fleet.on_driver_online(redis, JASHIM, at(120))
    assert await available(redis) == {JASHIM}


async def test_older_event_arriving_late_is_ignored(redis):
    await fleet.on_driver_online(redis, JASHIM, at(500))            # newest, processed first
    assert not await fleet.on_driver_offline(redis, JASHIM, at(100))  # older, e.g. back from the retry queue
    assert await available(redis) == {JASHIM}


async def test_concurrent_events_always_end_in_the_newest_state(redis):
    # The consumer handles up to 20 messages at once; the plan's read-then-write could let the older one win.
    for i in range(40):
        driver = f"d{i}"
        events = [fleet.on_driver_offline(redis, driver, at(0)), fleet.on_driver_online(redis, driver, at(1)),
                  fleet.on_driver_offline(redis, driver, at(2)), fleet.on_driver_online(redis, driver, at(3))]
        random.Random(i).shuffle(events)
        await asyncio.gather(*events)
        assert await redis.hget(h(driver), "online") == "1", i
        assert driver in await available(redis), i


# ---- pools --------------------------------------------------------------------------------------------------------

async def test_forming_pool_makes_busy_then_completed_frees(redis):
    await fleet.on_driver_online(redis, JASHIM, at(0))
    await fleet.on_pool_updated(redis, JASHIM, "bullet-1", "FORMING", at(100))
    assert await available(redis) == set() and await redis.hget(h(JASHIM), "pool_id") == "bullet-1"
    await fleet.on_pool_updated(redis, JASHIM, "bullet-1", "IN_PROGRESS", at(200))
    assert await available(redis) == set()
    await fleet.on_pool_updated(redis, JASHIM, "bullet-1", "COMPLETED", at(300))
    assert await available(redis) == {JASHIM} and await redis.hget(h(JASHIM), "pool_id") is None


async def test_cancelled_pool_also_frees(redis):
    await fleet.on_driver_online(redis, JASHIM, at(0))
    await fleet.on_pool_updated(redis, JASHIM, "bullet-1", "FORMING", at(100))
    await fleet.on_pool_updated(redis, JASHIM, "bullet-1", "CANCELLED", at(200))
    assert await available(redis) == {JASHIM}


async def test_stale_forming_after_completed_is_ignored(redis):
    # FORMING failed once and came back from the retry queue after COMPLETED was already applied.
    await fleet.on_driver_online(redis, JASHIM, at(0))
    await fleet.on_pool_updated(redis, JASHIM, "bullet-1", "COMPLETED", at(300))
    assert not await fleet.on_pool_updated(redis, JASHIM, "bullet-1", "FORMING", at(100))
    assert await available(redis) == {JASHIM}  # not stuck "busy" forever


async def test_end_of_an_old_pool_does_not_free_the_current_one(redis):
    await fleet.on_driver_online(redis, JASHIM, at(0))
    await fleet.on_pool_updated(redis, JASHIM, "bullet-2", "FORMING", at(500))
    await fleet.on_pool_updated(redis, JASHIM, "bullet-1", "COMPLETED", at(600))  # different pool
    assert await available(redis) == set() and await redis.hget(h(JASHIM), "pool_id") == "bullet-2"


async def test_pool_finished_while_offline_stays_unavailable(redis):
    await fleet.on_driver_online(redis, JASHIM, at(0))
    await fleet.on_pool_updated(redis, JASHIM, "bullet-1", "FORMING", at(100))
    await fleet.on_driver_offline(redis, JASHIM, at(200))
    await fleet.on_pool_updated(redis, JASHIM, "bullet-1", "COMPLETED", at(300))
    assert await available(redis) == set()


async def test_pool_event_before_online_event(redis):
    # Identity's and Trip's events are ordered separately (state_ts vs pool_ts): an "online" at t50 must still
    # apply after a pool event at t100, or Jashim would never become available when the pool ends.
    await fleet.on_pool_updated(redis, JASHIM, "bullet-1", "FORMING", at(100))
    assert await fleet.on_driver_online(redis, JASHIM, at(50))
    assert await available(redis) == set()  # online, but busy
    await fleet.on_pool_updated(redis, JASHIM, "bullet-1", "COMPLETED", at(200))
    assert await available(redis) == {JASHIM}


async def test_late_offline_still_applies_after_a_newer_pool_event(redis):
    await fleet.on_driver_online(redis, JASHIM, at(0))
    await fleet.on_pool_updated(redis, JASHIM, "bullet-1", "FORMING", at(100))
    assert await fleet.on_driver_offline(redis, JASHIM, at(50))  # newer than the last online/offline event
    await fleet.on_pool_updated(redis, JASHIM, "bullet-1", "COMPLETED", at(300))
    assert await available(redis) == set()  # he went offline: must not be offered rides


# ---- GPS pings ----------------------------------------------------------------------------------------------------

async def test_ping_records_position_zone_and_heartbeat(redis, dist):
    zone = await fleet.record_ping(redis, dist, JASHIM, 23.7937, 90.4066, at(0))
    assert zone == "BANANI"
    state = await redis.hgetall(h(JASHIM))
    assert (state["zone"], float(state["lat"]), state["ping_ts"]) == ("BANANI", 23.7937, at(0))
    assert await on_map(redis, JASHIM)
    assert 0 < await redis.ttl(f"{h(JASHIM)}:hb") <= 30


async def test_ping_with_passengers_is_broadcast(redis, dist):
    await fleet.on_driver_online(redis, JASHIM, at(0))
    await fleet.on_pool_updated(redis, JASHIM, "bullet-1", "IN_PROGRESS", at(10))
    sub = redis.pubsub()
    await sub.subscribe("loc:pool:bullet-1")
    await fleet.record_ping(redis, dist, JASHIM, 23.7806, 90.4163, at(20))
    msg = await next_message(sub, 1)
    assert json.loads(msg["data"]) == {"pool_id": "bullet-1", "driver_id": JASHIM, "lat": 23.7806, "lng": 90.4163,
                                       "zone": "GULSHAN_1", "ts": at(20)}
    await sub.aclose()


async def test_ping_without_pool_is_not_broadcast(redis, dist):
    sub = redis.pubsub()
    await sub.psubscribe("loc:pool:*")
    await fleet.record_ping(redis, dist, JASHIM, 23.7937, 90.4066, at(0))
    assert await next_message(sub, 0.3) is None
    await sub.aclose()


async def test_pinging_does_not_make_an_offline_driver_available(redis, dist):
    await fleet.record_ping(redis, dist, JASHIM, 23.7937, 90.4066, at(0))
    assert await available(redis) == set()


# ---- nearby search ------------------------------------------------------------------------------------------------

async def online_at(redis, dist, driver, lat, lng):
    await fleet.on_driver_online(redis, driver, at(0))
    await fleet.record_ping(redis, dist, driver, lat, lng, at(1))


async def search_banani(redis, dist, limit=5):
    lat, lng = dist.zones["BANANI"]
    return await fleet.nearby_available(redis, lat, lng, 3000, limit)


async def test_nearby_finds_free_driver_nearest_first(redis, dist):
    await online_at(redis, dist, KARIM, 23.7925, 90.4144)   # Gulshan 2, ~800 m away
    await online_at(redis, dist, JASHIM, 23.7940, 90.4070)  # ~50 m from Banani's centre
    found = await search_banani(redis, dist)
    assert [d for d, _ in found] == [JASHIM, KARIM]
    assert found[0][1] < 100 < found[1][1] < 3000


async def test_nearby_skips_far_busy_offline_and_silent_drivers(redis, dist):
    await online_at(redis, dist, "uttara", 23.8759, 90.3795)  # ~10 km away
    await online_at(redis, dist, "busy", 23.7937, 90.4066)
    await fleet.on_pool_updated(redis, "busy", "p", "FORMING", at(5))
    await online_at(redis, dist, "gone-home", 23.7937, 90.4066)
    await fleet.on_driver_offline(redis, "gone-home", at(5))
    await fleet.record_ping(redis, dist, "gone-home", 23.7937, 90.4066, at(6))  # app still pinging
    await online_at(redis, dist, "tunnel", 23.7937, 90.4066)
    await redis.delete(f"{h('tunnel')}:hb")  # heartbeat expired: no ping for 30 s
    assert await search_banani(redis, dist) == []


async def test_nearby_respects_limit(redis, dist):
    for i in range(6):
        await online_at(redis, dist, f"d{i}", 23.7937 + i * 0.001, 90.4066)
    assert [d for d, _ in await search_banani(redis, dist, limit=3)] == ["d0", "d1", "d2"]


async def test_nearby_on_empty_map(redis, dist):
    assert await search_banani(redis, dist) == []
