"""7.6: Jashim's GPS (Matching publishes on loc:pool:{pool_id}) reaches his pool's passengers, and only them."""
import asyncio
import json

import fakeredis
import pytest

from app import live_location
from app.connections import ConnectionManager
from app.consumers import members_key
from app.live_location import relay_locations
from conftest import JASHIM, NUSRAT, RAFIQ
from test_connections import FakeSocket

POOL = "pool-bullet"
PING = {"pool_id": POOL, "driver_id": JASHIM, "lat": 23.7937, "lng": 90.4066, "zone": "BANANI",
        "ts": "2026-09-24T08:41:05.000000Z"}  # exactly Matching's message (fleet.record_ping, 4.5)


@pytest.fixture
async def world():
    """fakeredis, a ConnectionManager with Nusrat, Rafiq and Shirin connected, and the relay running."""
    redis = fakeredis.aioredis.FakeRedis(decode_responses=True)
    manager = ConnectionManager()
    sockets = {u: FakeSocket() for u in (NUSRAT, RAFIQ, "shirin")}
    for u, s in sockets.items():
        await manager.connect(u, s)
    await redis.sadd(members_key(POOL), NUSRAT, RAFIQ)
    stop = asyncio.Event()
    task = asyncio.create_task(relay_locations(redis, manager, stop))
    while not (await redis.pubsub_numpat()):  # wait until the relay has subscribed
        await asyncio.sleep(0.01)
    yield redis, sockets, stop, task
    stop.set()
    await asyncio.wait_for(task, 3)
    await redis.aclose()


async def arrived(socket, count=1, timeout=2.0):
    for _ in range(int(timeout / 0.02)):
        if len(socket.sent) >= count:
            return socket.sent
        await asyncio.sleep(0.02)
    raise AssertionError(f"got {socket.sent}")


async def test_the_pools_passengers_see_the_car(world):
    redis, sockets, *_ = world
    await redis.publish(f"loc:pool:{POOL}", json.dumps(PING))
    for u in (NUSRAT, RAFIQ):
        assert (await arrived(sockets[u]))[0] == {"type": "vehicle.location", "data": {
            "lat": 23.7937, "lng": 90.4066, "zone": "BANANI", "ts": "2026-09-24T08:41:05.000000Z"}}


async def test_nobody_else_sees_it_and_no_driver_id_is_sent(world):
    redis, sockets, *_ = world
    await redis.publish(f"loc:pool:{POOL}", json.dumps(PING))
    await arrived(sockets[NUSRAT])
    await asyncio.sleep(0.1)
    assert sockets["shirin"].sent == []
    assert JASHIM not in json.dumps(sockets[NUSRAT].sent)


async def test_another_pool_goes_to_its_own_passengers(world):
    redis, sockets, *_ = world
    await redis.sadd(members_key("pool-toofan"), "shirin")
    await redis.publish("loc:pool:pool-toofan", json.dumps(PING | {"pool_id": "pool-toofan", "zone": "MIRPUR"}))
    assert (await arrived(sockets["shirin"]))[0]["data"]["zone"] == "MIRPUR"
    assert sockets[NUSRAT].sent == []


async def test_a_finished_pool_reaches_nobody(world):
    redis, sockets, *_ = world
    await redis.delete(members_key(POOL))  # what persist does when the pool completes
    await redis.publish(f"loc:pool:{POOL}", json.dumps(PING))
    await asyncio.sleep(0.2)
    assert all(s.sent == [] for s in sockets.values())


@pytest.mark.parametrize("bad", ["not json", json.dumps({"pool_id": POOL}), json.dumps(["a", "list"])])
async def test_a_bad_message_is_skipped_and_the_relay_keeps_going(world, bad, caplog):
    """The plan's relay stopped for good on the first bad message; live locations were gone until a restart."""
    redis, sockets, _, task = world
    await redis.publish(f"loc:pool:{POOL}", bad)
    await redis.publish(f"loc:pool:{POOL}", json.dumps(PING))
    assert len(await arrived(sockets[NUSRAT])) == 1 and not task.done()
    assert "skipped a bad location message" in caplog.text


async def test_stops_when_asked(world):
    _, _, stop, task = world
    stop.set()
    await asyncio.wait_for(task, 3)  # within its 1-second poll


async def test_redis_trouble_is_survived(monkeypatch, caplog):
    """A Redis error re-subscribes after a pause instead of ending the relay."""
    monkeypatch.setattr(live_location, "RETRY_SECONDS", 0.05)
    calls = {"n": 0}

    class BrokenPubSub:
        async def psubscribe(self, *_):
            calls["n"] += 1
            raise ConnectionError("redis down")

        async def punsubscribe(self, *_):
            pass

        async def aclose(self):
            pass

    class BrokenRedis:
        def pubsub(self):
            return BrokenPubSub()

    stop = asyncio.Event()
    task = asyncio.create_task(relay_locations(BrokenRedis(), ConnectionManager(), stop))
    await asyncio.sleep(0.3)
    assert calls["n"] >= 3 and not task.done()  # it kept trying
    stop.set()
    await asyncio.wait_for(task, 2)
    assert "re-subscribing" in caplog.text
