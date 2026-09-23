"""Step 2.6.6: forwarding, header hygiene (client X-User-* stripped), errors, health."""
import fakeredis
import httpx
import pytest

from app.main import app
from conftest import INTERNAL_TOKEN


async def test_client_sent_identity_headers_are_stripped(client, upstreams, auth_header):
    spoofed = {"X-User-Id": "jashim", "X-User-Role": "ADMIN", "X-User-Name": "Boss",
               "X-Token-Jti": "someone-elses-token", "X-Token-Exp": "0", "X-Internal-Token": "guess"}
    await client.get("/api/v1/users/me", headers=auth_header("nusrat") | spoofed)
    up = upstreams.last.headers
    assert up.get_list("x-user-id") == ["nusrat"]
    assert up.get_list("x-user-role") == ["PASSENGER"]
    assert up.get_list("x-user-name") == ["Nusrat"]
    assert up.get_list("x-token-jti") == ["jti-nusrat"]
    assert int(up["x-token-exp"]) > 0
    assert up.get_list("x-internal-token") == [INTERNAL_TOKEN]


async def test_public_route_forwards_no_identity(client, upstreams):
    resp = await client.get("/api/v1/zones", headers={"X-User-Id": "jashim", "X-User-Role": "ADMIN"})
    assert resp.status_code == 200
    up = upstreams.last.headers
    assert "x-user-id" not in up and "x-user-role" not in up and "x-token-jti" not in up
    assert up["x-internal-token"] == INTERNAL_TOKEN


async def test_raw_token_never_reaches_services(client, upstreams, auth_header):
    await client.get("/api/v1/users/me", headers=auth_header())
    assert "authorization" not in upstreams.last.headers


async def test_prefix_stripped_and_body_passed_through(client, upstreams, auth_header):
    body = b'{"lat":23.7937,"lng":90.4066}'
    await client.post("/api/v1/driver/location", content=body,
                      headers=auth_header("jashim", "DRIVER") | {"Content-Type": "application/json"})
    up = upstreams.last
    assert (up.url.host, up.url.path, up.content) == ("matching", "/driver/location", body)


async def test_repeated_query_params_survive(client, upstreams, auth_header):
    await client.get("/api/v1/notifications?tag=a&tag=b&limit=50", headers=auth_header())
    assert upstreams.last.url.query == b"tag=a&tag=b&limit=50"


async def test_request_id_passed_once_and_returned(client, upstreams, auth_header):
    resp = await client.get("/api/v1/users/me", headers=auth_header() | {"X-Request-Id": "req-42"})
    assert upstreams.last.headers.get_list("x-request-id") == ["req-42"]
    assert resp.headers.get_list("x-request-id") == ["req-42"]


async def test_request_id_generated_when_missing(client, upstreams, auth_header):
    resp = await client.get("/api/v1/users/me", headers=auth_header())
    generated = resp.headers["x-request-id"]
    assert len(generated) == 36 and upstreams.last.headers["x-request-id"] == generated


@pytest.mark.parametrize("path", [
    "/api/v1/zones/%2e%2e/internal/match/evaluate",      # public route -> Matching internals
    "/api/v1/rides/%2E%2E/internal/drivers/x/live-pool",  # logged-in route -> Trip internals
    "/api/v1/zones/%2e/x",
])
async def test_dot_segments_blocked_before_any_upstream(client, upstreams, auth_header, path):
    resp = await client.get(path, headers=auth_header())
    assert resp.status_code == 404
    assert not upstreams.calls


async def test_unknown_route_404(client, upstreams, auth_header):
    resp = await client.get("/api/v1/nope", headers=auth_header())
    assert resp.status_code == 404 and resp.json()["error"]["code"] == "NOT_FOUND"
    assert not upstreams.calls


async def test_protected_route_without_token_401(client, upstreams):
    resp = await client.get("/api/v1/rides")
    assert resp.status_code == 401 and resp.json()["error"]["code"] == "UNAUTHENTICATED"
    assert not upstreams.calls


async def test_upstream_status_and_body_passed_back(client, upstreams, auth_header):
    upstreams.handlers[("POST", "/rides/r1/cancel")] = lambda r: httpx.Response(
        409, json={"error": {"code": "CANNOT_CANCEL"}}, headers={"X-Custom": "yes"})
    resp = await client.post("/api/v1/rides/r1/cancel", headers=auth_header())
    assert resp.status_code == 409 and resp.json()["error"]["code"] == "CANNOT_CANCEL"
    assert resp.headers["x-custom"] == "yes"


async def test_upstream_down_503(client, upstreams, auth_header):
    upstreams.down.add("fare")
    resp = await client.get("/api/v1/wallet", headers=auth_header())
    assert resp.status_code == 503 and resp.json()["error"]["code"] == "UPSTREAM_UNAVAILABLE"


async def test_upstream_timeout_504(client, upstreams, auth_header):
    upstreams.slow.add("fare")
    resp = await client.get("/api/v1/wallet", headers=auth_header())
    assert resp.status_code == 504 and resp.json()["error"]["code"] == "UPSTREAM_TIMEOUT"


async def test_redis_down_fails_closed_with_503(client, upstreams):
    server = fakeredis.FakeServer()
    server.connected = False
    app.state.redis = fakeredis.aioredis.FakeRedis(server=server, decode_responses=True)
    resp = await client.get("/api/v1/zones")  # even public routes need the rate limiter
    assert resp.status_code == 503
    assert resp.json()["error"]["code"] == "DEPENDENCY_UNAVAILABLE"
    assert not upstreams.calls


async def test_health_all_ok(client):
    resp = await client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok", "checks": {name: "ok" for name in
                                                      ("redis", "identity", "matching", "trip", "fare", "notification")}}


async def test_health_degraded_names_the_broken_service(client, upstreams):
    upstreams.down.add("trip")
    resp = await client.get("/health")
    assert resp.status_code == 503
    checks = resp.json()["checks"]
    assert checks["trip"].startswith("fail") and checks["fare"] == "ok"
