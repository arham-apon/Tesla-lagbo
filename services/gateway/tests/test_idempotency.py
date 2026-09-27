"""Step 2.6.5: Idempotency-Key replay, reuse (422), 5xx deletes the key."""
import hashlib
import json

import httpx
import pytest

RIDE = b'{"pickup_zone":"BANANI","dropoff_zone":"MOHAKHALI","seats":1}'
JSON = {"Content-Type": "application/json"}


@pytest.fixture
def trip_creates_rides(upstreams):
    def create(request: httpx.Request) -> httpx.Response:
        if b'"boom"' in request.content:
            return httpx.Response(500, json={"error": "trip exploded"})
        return httpx.Response(201, json={"ride_id": f"ride-{len(upstreams.calls)}", "status": "REQUESTED"})
    upstreams.handlers[("POST", "/rides")] = create


def headers(auth_header, key, user="nusrat"):
    return auth_header(user) | JSON | ({"Idempotency-Key": key} if key else {})


async def test_same_key_same_body_is_replayed(client, upstreams, auth_header, trip_creates_rides, redis):
    first = await client.post("/api/v1/rides", content=RIDE, headers=headers(auth_header, "tap-1"))
    second = await client.post("/api/v1/rides", content=RIDE, headers=headers(auth_header, "tap-1"))
    assert first.status_code == second.status_code == 201
    assert second.json() == first.json()
    assert second.headers["idempotent-replay"] == "true"
    assert "idempotent-replay" not in first.headers
    assert len(upstreams.calls) == 1  # Nusrat's double tap created one ride
    stored = json.loads(await redis.get("idem:nusrat:tap-1"))
    assert stored["state"] == "DONE" and stored["status"] == 201
    assert 86_000 < await redis.ttl("idem:nusrat:tap-1") <= 86_400


async def test_same_key_different_body_is_422(client, auth_header, trip_creates_rides):
    await client.post("/api/v1/rides", content=RIDE, headers=headers(auth_header, "tap-1"))
    resp = await client.post("/api/v1/rides", content=RIDE.replace(b'"seats":1', b'"seats":2'),
                             headers=headers(auth_header, "tap-1"))
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "IDEMPOTENCY_KEY_REUSED"


async def test_upstream_5xx_deletes_key_so_retry_goes_through(client, upstreams, auth_header, trip_creates_rides, redis):
    boom = b'{"boom":true}'
    resp = await client.post("/api/v1/rides", content=boom, headers=headers(auth_header, "tap-5xx"))
    assert resp.status_code == 500
    assert not await redis.exists("idem:nusrat:tap-5xx")
    await client.post("/api/v1/rides", content=boom, headers=headers(auth_header, "tap-5xx"))
    assert len(upstreams.calls) == 2  # retried, not replayed


async def test_upstream_down_deletes_key(client, upstreams, auth_header, redis):
    upstreams.down.add("trip")
    resp = await client.post("/api/v1/rides", content=RIDE, headers=headers(auth_header, "tap-down"))
    assert resp.status_code == 503
    assert resp.json()["error"]["code"] == "UPSTREAM_UNAVAILABLE"
    assert not await redis.exists("idem:nusrat:tap-down")


async def test_4xx_is_remembered(client, upstreams, auth_header):
    upstreams.handlers[("POST", "/rides")] = lambda r: httpx.Response(409, json={"error": {"code": "ACTIVE_RIDE_EXISTS"}})
    await client.post("/api/v1/rides", content=RIDE, headers=headers(auth_header, "tap-409"))
    again = await client.post("/api/v1/rides", content=RIDE, headers=headers(auth_header, "tap-409"))
    assert again.status_code == 409 and again.headers["idempotent-replay"] == "true"
    assert len(upstreams.calls) == 1


async def test_still_pending_is_409(client, upstreams, auth_header, redis):
    fp = hashlib.sha256(b"POST" + b"/api/v1/rides" + RIDE).hexdigest()
    await redis.set("idem:nusrat:tap-slow", json.dumps({"state": "PENDING", "fp": fp}), ex=120)
    resp = await client.post("/api/v1/rides", content=RIDE, headers=headers(auth_header, "tap-slow"))
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "IDEMPOTENCY_IN_PROGRESS"
    assert not upstreams.calls


async def test_key_required_on_create_ride(client, upstreams, auth_header):
    resp = await client.post("/api/v1/rides", content=RIDE, headers=headers(auth_header, None))
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "IDEMPOTENCY_KEY_REQUIRED"
    assert not upstreams.calls


async def test_key_optional_elsewhere(client, upstreams, auth_header):
    resp = await client.post("/api/v1/rides/r1/cancel", headers=auth_header())
    assert resp.status_code == 200 and len(upstreams.calls) == 1


async def test_keys_are_per_user(client, upstreams, auth_header, trip_creates_rides):
    await client.post("/api/v1/rides", content=RIDE, headers=headers(auth_header, "tap-1", "nusrat"))
    rafiq = await client.post("/api/v1/rides", content=RIDE, headers=headers(auth_header, "tap-1", "rafiq"))
    assert rafiq.status_code == 201 and "idempotent-replay" not in rafiq.headers
    assert len(upstreams.calls) == 2


async def test_get_is_never_tracked(client, upstreams, auth_header, redis):
    for _ in range(2):
        await client.get("/api/v1/rides/r1", headers=auth_header() | {"Idempotency-Key": "k"})
    assert len(upstreams.calls) == 2
    assert not await redis.exists("idem:nusrat:k")
