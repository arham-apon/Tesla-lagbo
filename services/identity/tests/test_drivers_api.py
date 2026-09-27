"""3.4: driver profile, vehicle upsert, online/offline (events via the outbox, offline check via Trip)."""
import json

import pytest
from sqlalchemy import select

from app.models import Driver, Outbox, User
from conftest import as_user

JASHIM = "11111111-1111-4111-8111-111111111111"
KARIM = "55555555-5555-4555-8555-555555555555"
BULLET = {"nickname": "Bullet", "make": "Tesla", "model": "Model Y", "plate": "DHAKA-TESLA-11", "seat_capacity": 3}
J = as_user(JASHIM, "DRIVER", "Jashim")


@pytest.fixture
async def drivers(db):
    async with db.rw.begin() as s:
        for uid, name, phone, lic in [(JASHIM, "Jashim", "01711000001", "DK-0001"),
                                      (KARIM, "Karim", "01711000005", "DK-0005")]:
            s.add(User(id=uid, full_name=name, phone=phone, password_hash="x", role="DRIVER"))
            s.add(Driver(user_id=uid, license_number=lic))


async def events(db) -> list[dict]:
    async with db.ro() as s:
        rows = (await s.execute(select(Outbox).order_by(Outbox.id))).scalars().all()
    return [json.loads(r.payload) for r in rows]


async def online_jashim(api):
    await api.put("/drivers/me/vehicle", json=BULLET, headers=J)
    assert (await api.post("/drivers/me/online", headers=J)).status_code == 200


async def test_passenger_cannot_go_online(api):
    resp = await api.post("/drivers/me/online", headers=as_user("nusrat", "PASSENGER"))
    assert resp.status_code == 403 and resp.json()["error"]["code"] == "FORBIDDEN"


async def test_profile_starts_offline_without_vehicle(api, drivers):
    body = (await api.get("/drivers/me", headers=J)).json()
    assert (body["status"], body["vehicle"], body["user"]["full_name"]) == ("OFFLINE", None, "Jashim")


async def test_online_without_vehicle_409(api, drivers, db):
    resp = await api.post("/drivers/me/online", headers=J)
    assert resp.status_code == 409 and resp.json()["error"]["code"] == "NO_VEHICLE"
    assert await events(db) == []


async def test_vehicle_upsert_keeps_same_vehicle(api, drivers):
    first = (await api.put("/drivers/me/vehicle", json=BULLET, headers=J)).json()
    second = (await api.put("/drivers/me/vehicle", json=BULLET | {"seat_capacity": 4}, headers=J)).json()
    assert first["id"] == second["id"] and second["seat_capacity"] == 4
    assert (await api.get("/drivers/me", headers=J)).json()["vehicle"]["seat_capacity"] == 4


async def test_plate_taken_by_another_driver_409(api, drivers):
    await api.put("/drivers/me/vehicle", json=BULLET, headers=J)
    resp = await api.put("/drivers/me/vehicle", json=BULLET | {"nickname": "Copy"}, headers=as_user(KARIM, "DRIVER"))
    assert resp.status_code == 409 and resp.json()["error"]["code"] == "PLATE_TAKEN"


async def test_bad_seat_capacity_422(api, drivers):
    resp = await api.put("/drivers/me/vehicle", json=BULLET | {"seat_capacity": 9}, headers=J)
    assert resp.status_code == 422


async def test_going_online_announces_bullet(api, drivers, db):
    await api.put("/drivers/me/vehicle", json=BULLET, headers=J)
    resp = await api.post("/drivers/me/online", headers=J)
    assert resp.status_code == 200 and resp.json()["status"] == "ONLINE"
    [event] = await events(db)
    assert event["event_type"] == "identity.driver.online" and event["producer"] == "identity-service"
    data = event["data"]
    assert (data["driver_id"], data["driver_name"], data["vehicle_nickname"], data["plate"], data["seat_capacity"]) == \
           (JASHIM, "Jashim", "Bullet", "DHAKA-TESLA-11", 3)


async def test_online_twice_emits_once(api, drivers, db):
    await online_jashim(api)
    assert (await api.post("/drivers/me/online", headers=J)).status_code == 200
    assert len(await events(db)) == 1


async def test_vehicle_locked_while_online(api, drivers):
    await online_jashim(api)
    resp = await api.put("/drivers/me/vehicle", json=BULLET | {"seat_capacity": 2}, headers=J)
    assert resp.status_code == 409 and resp.json()["error"]["code"] == "DRIVER_ONLINE"
    assert (await api.get("/drivers/me", headers=J)).json()["vehicle"]["seat_capacity"] == 3


async def test_offline_when_trip_says_no_pool(api, drivers, db, trip):
    await online_jashim(api)
    resp = await api.post("/drivers/me/offline", headers=J | {"X-Request-Id": "req-7"})
    assert resp.status_code == 200 and resp.json()["status"] == "OFFLINE"
    call = trip.calls[-1]
    assert call.url.path == f"/internal/drivers/{JASHIM}/live-pool"
    assert call.headers["x-internal-token"] and call.headers["x-request-id"] == "req-7"
    assert [e["event_type"] for e in await events(db)] == ["identity.driver.online", "identity.driver.offline"]
    assert (await events(db))[-1]["data"] == {"driver_id": JASHIM}


async def test_offline_refused_with_passengers_aboard(api, drivers, db, trip):
    await online_jashim(api)
    trip.pool_id = "pool-banani-1"
    resp = await api.post("/drivers/me/offline", headers=J)
    assert resp.status_code == 409 and resp.json()["error"]["code"] == "DRIVER_HAS_LIVE_POOL"
    assert (await api.get("/drivers/me", headers=J)).json()["status"] == "ONLINE"
    assert len(await events(db)) == 1  # no offline event


@pytest.mark.parametrize("broken", ["down", 500, 401, 404])
async def test_offline_fails_closed_when_trip_cannot_answer(api, drivers, db, trip, broken):
    await online_jashim(api)
    if broken == "down":
        trip.down = True
    else:
        trip.status = broken
    resp = await api.post("/drivers/me/offline", headers=J)
    assert resp.status_code == 503
    assert (await api.get("/drivers/me", headers=J)).json()["status"] == "ONLINE"
    assert len(await events(db)) == 1


async def test_offline_when_already_offline_emits_nothing(api, drivers, db):
    resp = await api.post("/drivers/me/offline", headers=J)
    assert resp.status_code == 200 and await events(db) == []


async def test_driver_endpoints_need_gateway(api, drivers):
    resp = await api.get("/drivers/me", headers={k: v for k, v in J.items() if k != "X-Internal-Token"})
    assert resp.status_code == 401
