"""Step 2.6.4: fixed-window rate limit (11th login in one minute -> 429)."""
from types import SimpleNamespace

import pytest

from tesla_common.errors import DomainError

from app import ratelimit

NOW = 1_790_000_000.0  # fixed clock, so the test can't straddle a minute boundary


@pytest.fixture
def clock(monkeypatch):
    t = SimpleNamespace(now=NOW)
    monkeypatch.setattr(ratelimit, "time", SimpleNamespace(time=lambda: t.now))
    return t


async def test_eleventh_login_in_a_minute_is_429(client, upstreams, clock):
    codes = [(await client.post("/api/v1/auth/login", json={})).status_code for _ in range(10)]
    assert codes == [200] * 10
    resp = await client.post("/api/v1/auth/login", json={})
    assert resp.status_code == 429
    assert resp.json()["error"]["code"] == "RATE_LIMITED"
    assert len(upstreams.calls) == 10  # the 11th never reached Identity


async def test_next_minute_starts_fresh(client, clock):
    for _ in range(11):
        await client.post("/api/v1/auth/login", json={})
    clock.now += 60
    assert (await client.post("/api/v1/auth/login", json={})).status_code == 200


async def test_counters_are_per_client_and_per_route(redis, clock):
    for _ in range(2):
        await ratelimit.enforce(redis, "/api/v1/rides:nusrat", 2)
    with pytest.raises(DomainError) as exc:
        await ratelimit.enforce(redis, "/api/v1/rides:nusrat", 2)
    assert exc.value.status == 429
    await ratelimit.enforce(redis, "/api/v1/rides:rafiq", 2)          # other user: unaffected
    await ratelimit.enforce(redis, "/api/v1/notifications:nusrat", 2)  # other route: unaffected


async def test_counter_key_expires(redis, clock):
    await ratelimit.enforce(redis, "k", 5)
    key = f"rl:k:{int(NOW // 60)}"
    assert await redis.get(key) == "1"
    assert 0 < await redis.ttl(key) <= 60


async def test_authenticated_routes_count_per_user(client, auth_header, redis, clock):
    await client.get("/api/v1/users/me", headers=auth_header("rafiq"))
    assert await redis.get(f"rl:/api/v1/users:rafiq:{int(NOW // 60)}") == "1"
