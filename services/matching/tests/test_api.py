"""4.6: GET /zones, POST /driver/location, GET /internal/zones/distance, POST /internal/match/evaluate."""
import pytest

from app import fleet
from conftest import GATEWAY, as_user

BANANI = (23.7937, 90.4066)
NUSRAT_POOL = {"pool_id": "bullet-1", "driver_id": "jashim", "pickup_zone": "BANANI", "remaining_seats": 2,
               "version": 1, "stops": [{"ride_id": "nusrat", "kind": "PICKUP", "zone": "BANANI"},
                                       {"ride_id": "nusrat", "kind": "DROPOFF", "zone": "MOHAKHALI"}]}


async def ping(api, driver: str, lat: float, lng: float):
    return await api.post("/driver/location", json={"lat": lat, "lng": lng}, headers=as_user(driver))


async def evaluate(api, body: dict):
    return await api.post("/internal/match/evaluate", json=body, headers=GATEWAY)


# ---- GET /zones ---------------------------------------------------------------------------------------------------

async def test_zones_list(api):
    resp = await api.get("/zones", headers=GATEWAY)
    assert resp.status_code == 200
    zones = resp.json()
    assert len(zones) == 9 and zones[0] == {"code": "BANANI", "name": "Banani", "lat": 23.7937, "lng": 90.4066}
    assert [z["name"] for z in zones] == sorted(z["name"] for z in zones)


async def test_zones_need_no_login_but_must_come_through_gateway(api):
    assert (await api.get("/zones", headers=GATEWAY)).status_code == 200  # no X-User-* at all
    assert (await api.get("/zones")).status_code == 401


# ---- POST /driver/location ----------------------------------------------------------------------------------------

async def test_ping_returns_zone_and_records_it(api, redis):
    resp = await ping(api, "jashim", *BANANI)
    assert resp.status_code == 202 and resp.json() == {"zone": "BANANI"}
    state = await redis.hgetall(fleet.h("jashim"))
    assert state["zone"] == "BANANI" and state["ping_ts"].endswith("Z") and "." in state["ping_ts"]


async def test_passenger_cannot_send_driver_location(api):
    resp = await api.post("/driver/location", json={"lat": BANANI[0], "lng": BANANI[1]},
                          headers=as_user("nusrat", "PASSENGER"))
    assert resp.status_code == 403 and resp.json()["error"]["code"] == "FORBIDDEN"


@pytest.mark.parametrize("lat, lng", [(22.3569, 91.7832), (0.0, 0.0)])
async def test_ping_outside_dhaka_422(api, lat, lng):
    resp = await ping(api, "jashim", lat, lng)
    assert resp.status_code == 422 and resp.json()["error"]["code"] == "VALIDATION_ERROR"


async def test_ping_needs_gateway(api):
    resp = await api.post("/driver/location", json={"lat": BANANI[0], "lng": BANANI[1]},
                          headers={"X-User-Id": "jashim", "X-User-Role": "DRIVER"})
    assert resp.status_code == 401


# ---- GET /internal/zones/distance ---------------------------------------------------------------------------------

async def test_distance(api):
    resp = await api.get("/internal/zones/distance", params={"from": "BANANI", "to": "MOHAKHALI"}, headers=GATEWAY)
    assert resp.status_code == 200 and resp.json() == {"distance_m": 3500}


async def test_distance_unknown_zone_422(api):
    resp = await api.get("/internal/zones/distance", params={"from": "BANANI", "to": "MOTIJHEEL"}, headers=GATEWAY)
    assert resp.status_code == 422 and resp.json()["error"]["code"] == "UNKNOWN_ZONE"


async def test_distance_missing_param_422(api):
    resp = await api.get("/internal/zones/distance", params={"from": "BANANI"}, headers=GATEWAY)
    assert resp.status_code == 422


async def test_internal_needs_internal_token(api):
    resp = await api.get("/internal/zones/distance", params={"from": "BANANI", "to": "MOHAKHALI"})
    assert resp.status_code == 401 and resp.json()["error"]["code"] == "UNAUTHORIZED_INTERNAL"
    resp = await api.get("/internal/zones/distance", params={"from": "BANANI", "to": "MOHAKHALI"},
                         headers={"X-Internal-Token": "guess"})
    assert resp.status_code == 401


# ---- POST /internal/match/evaluate: the Banani story -------------------------------------------------------------

async def test_nusrat_no_pools_jashim_is_the_candidate(api, redis):
    await fleet.on_driver_online(redis, "jashim", "2026-09-24T08:40:00.000000Z")
    await ping(api, "jashim", 23.7940, 90.4070)
    resp = await evaluate(api, {"pickup_zone": "BANANI", "dropoff_zone": "MOHAKHALI", "seats": 1})
    assert resp.status_code == 200
    body = resp.json()
    assert body["solo_distance_m"] == 3500 and body["compatible_pools"] == []
    assert [c["driver_id"] for c in body["candidate_drivers"]] == ["jashim"]
    assert body["candidate_drivers"][0]["distance_m"] < 100


async def test_rafiq_gets_bullet_and_jashim_is_busy(api, redis):
    await fleet.on_driver_online(redis, "jashim", "2026-09-24T08:40:00.000000Z")
    await ping(api, "jashim", *BANANI)
    await fleet.on_pool_updated(redis, "jashim", "bullet-1", "FORMING", "2026-09-24T08:41:00.000000Z")
    resp = await evaluate(api, {"pickup_zone": "BANANI", "dropoff_zone": "GULSHAN_1", "seats": 1,
                                "open_pools": [NUSRAT_POOL]})
    body = resp.json()
    [option] = body["compatible_pools"]
    assert (option["pool_id"], option["version"], option["added_route_m"], option["max_detour_pct"]) == \
           ("bullet-1", 1, 500, 114)
    assert [(s["kind"], s["zone"]) for s in option["plan"]] == [
        ("PICKUP", "BANANI"), ("PICKUP", "BANANI"), ("DROPOFF", "GULSHAN_1"), ("DROPOFF", "MOHAKHALI")]
    assert body["solo_distance_m"] == 2000
    assert body["candidate_drivers"] == []  # Jashim is in a pool now


async def test_shirin_two_seats_no_pool(api):
    full_ish = NUSRAT_POOL | {"remaining_seats": 1, "version": 2}
    body = (await evaluate(api, {"pickup_zone": "BANANI", "dropoff_zone": "GULSHAN_2", "seats": 2,
                                 "open_pools": [full_ish]})).json()
    assert body["compatible_pools"] == []


async def test_far_driver_is_not_a_candidate(api, redis):
    await fleet.on_driver_online(redis, "uttara-driver", "2026-09-24T08:40:00.000000Z")
    await ping(api, "uttara-driver", 23.8759, 90.3795)
    body = (await evaluate(api, {"pickup_zone": "BANANI", "dropoff_zone": "MOHAKHALI", "seats": 1})).json()
    assert body["candidate_drivers"] == []


async def test_max_candidates(api, redis):
    for i in range(4):
        await fleet.on_driver_online(redis, f"d{i}", "2026-09-24T08:40:00.000000Z")
        await ping(api, f"d{i}", BANANI[0] + i * 0.001, BANANI[1])
    body = (await evaluate(api, {"pickup_zone": "BANANI", "dropoff_zone": "MOHAKHALI", "seats": 1,
                                 "max_candidates": 2})).json()
    assert [c["driver_id"] for c in body["candidate_drivers"]] == ["d0", "d1"]


@pytest.mark.parametrize("body, code", [
    ({"pickup_zone": "BANANI", "dropoff_zone": "MOTIJHEEL", "seats": 1}, "UNKNOWN_ZONE"),
    ({"pickup_zone": "BANANI", "dropoff_zone": "BANANI", "seats": 1}, "VALIDATION_ERROR"),
    ({"pickup_zone": "BANANI", "dropoff_zone": "MOHAKHALI", "seats": 7}, "VALIDATION_ERROR"),
])
async def test_evaluate_bad_input_422(api, body, code):
    resp = await evaluate(api, body)
    assert resp.status_code == 422 and resp.json()["error"]["code"] == code


async def test_evaluate_needs_internal_token(api):
    resp = await api.post("/internal/match/evaluate",
                          json={"pickup_zone": "BANANI", "dropoff_zone": "MOHAKHALI", "seats": 1})
    assert resp.status_code == 401
