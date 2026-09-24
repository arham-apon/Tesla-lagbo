"""6.4: distances come from Matching and are cached in Redis for 24 h."""
import httpx
import pytest
import respx
from redis.exceptions import ConnectionError as RedisConnectionError

from tesla_common.errors import DomainError
from tesla_common.http import ServiceClient

from app.distance import CACHE_SECONDS, zone_distance
from conftest import INTERNAL_TOKEN

MATCHING = "http://matching.test"
ROUTE = f"{MATCHING}/internal/zones/distance"


@pytest.fixture
async def matching():
    http = ServiceClient(MATCHING, INTERNAL_TOKEN, "matching")
    yield http
    await http.aclose()


@respx.mock
async def test_asks_matching_then_caches_for_a_day(redis, matching):
    route = respx.get(ROUTE).mock(return_value=httpx.Response(200, json={"distance_m": 3500}))
    assert await zone_distance(redis, matching, "BANANI", "MOHAKHALI", "req-1") == 3500
    sent = route.calls.last.request
    assert (sent.url.params["from"], sent.url.params["to"]) == ("BANANI", "MOHAKHALI")
    assert sent.headers["x-internal-token"] == INTERNAL_TOKEN and sent.headers["x-request-id"] == "req-1"
    assert await redis.get("fare:dist:BANANI:MOHAKHALI") == "3500"
    assert 0 < await redis.ttl("fare:dist:BANANI:MOHAKHALI") <= CACHE_SECONDS == 86400


@respx.mock
async def test_second_ask_does_not_call_matching(redis, matching):
    route = respx.get(ROUTE).mock(return_value=httpx.Response(200, json={"distance_m": 3500}))
    for _ in range(3):
        assert await zone_distance(redis, matching, "BANANI", "MOHAKHALI", None) == 3500
    assert route.call_count == 1


@respx.mock
async def test_each_direction_is_its_own_entry(redis, matching):
    respx.get(ROUTE).mock(return_value=httpx.Response(200, json={"distance_m": 3500}))
    await zone_distance(redis, matching, "BANANI", "MOHAKHALI", None)
    await zone_distance(redis, matching, "MOHAKHALI", "BANANI", None)
    assert sorted(await redis.keys("fare:dist:*")) == ["fare:dist:BANANI:MOHAKHALI", "fare:dist:MOHAKHALI:BANANI"]


@respx.mock
async def test_unknown_zone(redis, matching):
    respx.get(ROUTE).mock(return_value=httpx.Response(
        422, json={"error": {"code": "UNKNOWN_ZONE", "message": "Unknown zone MOTIJHEEL"}}))
    with pytest.raises(DomainError) as e:
        await zone_distance(redis, matching, "BANANI", "MOTIJHEEL", None)
    assert (e.value.code, e.value.status, e.value.message) == ("UNKNOWN_ZONE", 422, "Unknown zone MOTIJHEEL")
    assert await redis.keys("fare:dist:*") == []  # nothing cached


@respx.mock
@pytest.mark.parametrize("status", [401, 404])
async def test_unexpected_answer_is_503_not_a_crash(redis, matching, status):
    respx.get(ROUTE).mock(return_value=httpx.Response(status, json={"error": {}}))
    with pytest.raises(DomainError) as e:
        await zone_distance(redis, matching, "BANANI", "MOHAKHALI", None)
    assert (e.value.code, e.value.status) == ("UPSTREAM_ERROR", 503)


@respx.mock
async def test_matching_down(redis, matching):
    respx.get(ROUTE).mock(side_effect=httpx.ConnectError("refused"))
    with pytest.raises(DomainError) as e:
        await zone_distance(redis, matching, "BANANI", "MOHAKHALI", None)
    assert (e.value.code, e.value.status) == ("UPSTREAM_UNAVAILABLE", 503)


@respx.mock
async def test_matching_is_retried_once(redis, matching):
    route = respx.get(ROUTE).mock(side_effect=[httpx.ReadTimeout("slow"),
                                               httpx.Response(200, json={"distance_m": 2000})])
    assert await zone_distance(redis, matching, "BANANI", "GULSHAN_1", None) == 2000
    assert route.call_count == 2


class BrokenRedis:
    async def get(self, key):
        raise RedisConnectionError("redis down")

    async def set(self, *a, **kw):
        raise RedisConnectionError("redis down")


@respx.mock
async def test_redis_down_still_gives_a_price(matching, caplog):
    respx.get(ROUTE).mock(return_value=httpx.Response(200, json={"distance_m": 3500}))
    assert await zone_distance(BrokenRedis(), matching, "BANANI", "MOHAKHALI", None) == 3500
    assert "distance cache unavailable" in caplog.text
