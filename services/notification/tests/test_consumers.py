"""7.6: persist (the durable inbox queue), push (the per-copy broadcast queue), and the pool member lists."""
import json

import fakeredis
import pytest
from redis.exceptions import ConnectionError as RedisConnectionError
from sqlalchemy import func, select

from app import consumers
from app.connections import ConnectionManager
from app.consumers import BINDINGS, MEMBERS_TTL_SECONDS, members_key
from app.models import Notification, ProcessedEvent
from conftest import JASHIM, NUSRAT, RAFIQ
from test_connections import FakeSocket
from test_routing import POOL, R_N, SHIRIN, cancelled, evening, matched, pool_updated, requested, settled


@pytest.fixture
async def redis():
    r = fakeredis.aioredis.FakeRedis(decode_responses=True)
    yield r
    await r.aclose()


async def inbox(db, user_id=None) -> list[tuple[str, str]]:
    q = select(Notification).order_by(Notification.id)
    if user_id:
        q = q.where(Notification.user_id == user_id)
    async with db.ro() as s:
        return [(n.user_id, n.type) for n in (await s.execute(q)).scalars()]


# ---- persist: the inbox -----------------------------------------------------------------------------------------

async def test_each_recipient_gets_an_inbox_row(db, redis):
    await consumers.persist(db.rw, redis, matched(R_N, NUSRAT, joined=False))
    assert await inbox(db) == [(NUSRAT, "ride.matched"), (JASHIM, "ride.matched")]


async def test_the_saved_payload_is_exactly_the_routed_message(db, redis):
    ev = settled(R_N, NUSRAT, 7200)
    await consumers.persist(db.rw, redis, ev)
    async with db.ro() as s:
        saved = (await s.execute(select(Notification.payload).where(Notification.user_id == NUSRAT))).scalar_one()
    from app.routing import recipients
    assert json.loads(saved) == recipients(ev)[0][1]  # what she'd have got live, allowlist and all


async def test_pool_updates_are_not_kept_in_the_inbox(db, redis):
    await consumers.persist(db.rw, redis, pool_updated([NUSRAT]))
    assert await inbox(db) == []


async def test_the_same_event_twice_is_saved_once(db, redis):
    ev = requested()
    await consumers.persist(db.rw, redis, ev)
    await consumers.persist(db.rw, redis, ev)
    assert len(await inbox(db)) == 2  # Jashim and Karim, once each


async def test_a_whole_evening(db, redis):
    for ev in evening():
        await consumers.persist(db.rw, redis, ev)
    assert [t for _, t in await inbox(db, NUSRAT)] == ["ride.matched", "ride.status", "ride.status", "fare.settled"]
    assert [t for _, t in await inbox(db, SHIRIN)] == ["ride.cancelled"]


# ---- the pool's member list (who may see the car move) -----------------------------------------------------------

async def test_members_follow_the_pool(db, redis):
    await consumers.persist(db.rw, redis, pool_updated([NUSRAT]))
    assert await redis.smembers(members_key(POOL)) == {NUSRAT}
    await consumers.persist(db.rw, redis, pool_updated([NUSRAT, RAFIQ]))
    assert await redis.smembers(members_key(POOL)) == {NUSRAT, RAFIQ}
    assert 0 < await redis.ttl(members_key(POOL)) <= MEMBERS_TTL_SECONDS
    await consumers.persist(db.rw, redis, pool_updated([RAFIQ], "IN_PROGRESS"))  # Nusrat dropped off
    assert await redis.smembers(members_key(POOL)) == {RAFIQ}


@pytest.mark.parametrize("status", ["COMPLETED", "CANCELLED"])
async def test_a_finished_pool_has_no_members(db, redis, status):
    await consumers.persist(db.rw, redis, pool_updated([NUSRAT, RAFIQ]))
    await consumers.persist(db.rw, redis, pool_updated([NUSRAT, RAFIQ], status))
    assert not await redis.exists(members_key(POOL))


class FlakyRedis:
    """Redis that fails the first pipeline, then works (a blip)."""
    def __init__(self, real):
        self.real, self.failures = real, 1

    def pipeline(self, transaction=True):
        if self.failures:
            self.failures -= 1
            raise RedisConnectionError("redis blip")
        return self.real.pipeline(transaction=transaction)


async def test_a_redis_blip_does_not_lose_the_member_list(db, redis):
    """Finding 3: in the plan, the event was already marked processed when Redis failed, so the retry skipped it
    and the passengers never saw the car move. Now the failure rolls everything back and the retry does it all."""
    ev = pool_updated([NUSRAT, RAFIQ])
    flaky = FlakyRedis(redis)
    with pytest.raises(RedisConnectionError):
        await consumers.persist(db.rw, flaky, ev)  # the bus would retry this in 5 s
    async with db.ro() as s:
        assert await s.get(ProcessedEvent, ev["event_id"]) is None
    await consumers.persist(db.rw, flaky, ev)  # the retry
    assert await redis.smembers(members_key(POOL)) == {NUSRAT, RAFIQ}


async def test_a_redis_blip_leaves_no_half_saved_inbox(db, redis):
    ev = cancelled(R_N, NUSRAT, POOL, JASHIM, "DRIVER")  # not a pool event: Redis isn't touched at all
    await consumers.persist(db.rw, FlakyRedis(redis), ev)
    assert len(await inbox(db)) == 2


async def test_a_broken_event_raises_and_leaves_nothing(db, redis):
    ev = matched(R_N, NUSRAT, joined=False)
    del ev["data"]["passenger_id"]
    with pytest.raises(KeyError):
        await consumers.persist(db.rw, redis, ev)  # retried, then dead-lettered by the bus
    async with db.ro() as s:
        assert await s.scalar(select(func.count()).select_from(ProcessedEvent)) == 0
    assert await inbox(db) == []


# ---- push: live, to the sockets this copy holds -----------------------------------------------------------------

async def test_push_reaches_open_sockets():
    manager, nusrat, jashim = ConnectionManager(), FakeSocket(), FakeSocket()
    await manager.connect(NUSRAT, nusrat)
    await manager.connect(JASHIM, jashim)
    await consumers.push(manager, settled(R_N, NUSRAT, 7200))
    assert nusrat.sent[0]["data"]["total_poysha"] == 7200
    assert jashim.sent[0]["data"] == {"ride_id": R_N, "total_poysha": 7200, "payment_method": "WALLET",
                                      "payment_status": "PAID"}


async def test_push_to_nobody_online_is_fine():
    await consumers.push(ConnectionManager(), requested())


# ---- wiring -----------------------------------------------------------------------------------------------------

async def test_both_queues_with_the_plans_bindings(db, redis):
    class RecordingBus:
        durable, broadcast = {}, []

        async def consume(self, queue, bindings, handler, **kw):
            self.durable[queue] = bindings

        async def consume_broadcast(self, bindings, handler):
            self.broadcast.append(bindings)

    bus = RecordingBus()
    await consumers.start(bus, db.rw, redis, ConnectionManager())
    assert bus.durable == {"notification.inbox": BINDINGS} and bus.broadcast == [BINDINGS]
    assert BINDINGS == ["trip.ride.*", "trip.pool.updated", "fare.ride.settled"]  # the plan's queue table (0.4)
