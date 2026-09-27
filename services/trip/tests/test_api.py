"""5.6: every endpoint in the plan's table, through HTTP, as the gateway would call it."""
from datetime import timedelta

import pytest
from sqlalchemy import select

from tesla_common.timeutil import utcnow

from app.models import RideOffer, RideRequest
from conftest import (AS_JASHIM, AS_NUSRAT, AS_RAFIQ, GATEWAY, JASHIM, KARIM, add, as_user,
                      bullet_with_nusrat, ride, shift)

TO_MOHAKHALI = {"pickup_zone": "BANANI", "dropoff_zone": "MOHAKHALI", "seats": 1}
TO_GULSHAN = {"pickup_zone": "BANANI", "dropoff_zone": "GULSHAN_1", "seats": 1}


def error(resp) -> tuple[int, str]:
    return resp.status_code, resp.json()["error"]["code"]


# ---- POST /rides ------------------------------------------------------------------------------------------------

async def test_request_with_no_pool_waits_for_a_driver(api, matching):
    matching.candidates = [(JASHIM, 800)]
    resp = await api.post("/rides", json=TO_MOHAKHALI, headers=as_user(AS_NUSRAT))
    assert resp.status_code == 201
    body = resp.json()
    assert (body["status"], body["pool_id"], body["driver"], body["estimated_fare_poysha"]) == \
           ("REQUESTED", None, None, 11000)


async def test_request_joins_bullet(api, db):
    pool_id, _ = await bullet_with_nusrat(db)
    resp = await api.post("/rides", json=TO_GULSHAN, headers=as_user(AS_RAFIQ))
    assert resp.status_code == 201
    body = resp.json()
    assert (body["status"], body["pool_id"]) == ("MATCHED", pool_id)
    assert body["driver"] == {"driver_id": JASHIM, "driver_name": "Jashim", "vehicle_nickname": "Bullet"}


async def test_second_active_ride_409(api):
    assert (await api.post("/rides", json=TO_MOHAKHALI, headers=as_user(AS_NUSRAT))).status_code == 201
    assert error(await api.post("/rides", json=TO_GULSHAN, headers=as_user(AS_NUSRAT))) == \
           (409, "ACTIVE_RIDE_EXISTS")


@pytest.mark.parametrize("body", [TO_MOHAKHALI | {"dropoff_zone": "BANANI"}, TO_MOHAKHALI | {"seats": 7},
                                  TO_MOHAKHALI | {"pickup_zone": "banani"}, {}])
async def test_bad_request_422(api, body):
    assert error(await api.post("/rides", json=body, headers=as_user(AS_NUSRAT))) == (422, "VALIDATION_ERROR")


async def test_only_passengers_request_rides(api):
    assert error(await api.post("/rides", json=TO_MOHAKHALI, headers=as_user(AS_JASHIM))) == (403, "FORBIDDEN")


async def test_must_come_through_the_gateway(api):
    no_token = {k: v for k, v in as_user(AS_NUSRAT).items() if k != "X-Internal-Token"}
    assert (await api.post("/rides", json=TO_MOHAKHALI, headers=no_token)).status_code == 401
    assert (await api.post("/rides", json=TO_MOHAKHALI, headers=GATEWAY)).status_code == 401  # no user


# ---- GET /rides, GET /rides/{id} --------------------------------------------------------------------------------

async def rides_at(db, *ages_and_statuses):
    """Nusrat's past rides, created `minutes` ago. Returns their ids, oldest first."""
    rows = [ride(created_at=utcnow() - timedelta(minutes=m), status=st) for m, st in ages_and_statuses]
    await add(db, *rows)
    return [r.id for r in rows]


async def test_my_rides_newest_first(api, db):
    old, mid, new = await rides_at(db, (30, "CANCELLED"), (20, "CANCELLED"), (10, "REQUESTED"))
    await add(db, ride(AS_RAFIQ.user_id, passenger_name="Rafiq"))  # not hers
    resp = await api.get("/rides", headers=as_user(AS_NUSRAT))
    assert resp.status_code == 200 and [r["id"] for r in resp.json()] == [new, mid, old]


async def test_filter_and_pages(api, db):
    old, mid, new = await rides_at(db, (30, "CANCELLED"), (20, "CANCELLED"), (10, "REQUESTED"))
    me = as_user(AS_NUSRAT)
    assert [r["id"] for r in (await api.get("/rides?status=CANCELLED", headers=me)).json()] == [mid, old]
    page1 = (await api.get("/rides?limit=2", headers=me)).json()
    assert [r["id"] for r in page1] == [new, mid]
    page2 = (await api.get("/rides", params={"limit": 2, "before": page1[-1]["created_at"]}, headers=me)).json()
    assert [r["id"] for r in page2] == [old]


async def test_before_with_a_timezone_is_converted(api, db):
    """The app may send Dhaka time. The ride is 60 min old; "before 30 min ago" finds it, "before 90 min ago"
    doesn't. Just dropping the "+06:00" would read 90 min ago as 4.5 h in the future and wrongly find it."""
    (only,) = await rides_at(db, (60, "CANCELLED"))

    def dhaka(minutes_ago: int) -> str:
        return (utcnow() + timedelta(hours=6) - timedelta(minutes=minutes_ago)).isoformat() + "+06:00"

    me = as_user(AS_NUSRAT)
    assert [r["id"] for r in (await api.get("/rides", params={"before": dhaka(30)}, headers=me)).json()] == [only]
    assert (await api.get("/rides", params={"before": dhaka(90)}, headers=me)).json() == []


@pytest.mark.parametrize("query", ["limit=0", "limit=101", "status=FLYING", "before=yesterday"])
async def test_bad_list_query_422(api, query):
    assert (await api.get(f"/rides?{query}", headers=as_user(AS_NUSRAT))).status_code == 422


async def test_ride_detail_with_history_and_driver(api, db):
    _, nusrat = await bullet_with_nusrat(db)
    resp = await api.get(f"/rides/{nusrat}", headers=as_user(AS_NUSRAT))
    assert resp.status_code == 200
    body = resp.json()
    assert body["driver"]["vehicle_nickname"] == "Bullet"
    assert [(h["from_status"], h["to_status"], h["actor_role"]) for h in body["history"]] == \
           [("REQUESTED", "MATCHED", "DRIVER")]
    assert all("actor_id" not in h for h in body["history"])


async def test_someone_elses_ride_is_404(api, db):
    _, nusrat = await bullet_with_nusrat(db)
    assert error(await api.get(f"/rides/{nusrat}", headers=as_user(AS_RAFIQ))) == (404, "RIDE_NOT_FOUND")


# ---- POST /rides/{id}/cancel ------------------------------------------------------------------------------------

async def test_passenger_cancels(api, db):
    _, nusrat = await bullet_with_nusrat(db)
    resp = await api.post(f"/rides/{nusrat}/cancel", json={}, headers=as_user(AS_NUSRAT))
    assert resp.status_code == 200
    assert (resp.json()["status"], resp.json()["cancel_reason"]) == ("CANCELLED", "changed_plans")


async def test_passenger_cannot_cancel_once_jashim_arrived(api, db):
    _, nusrat = await bullet_with_nusrat(db)
    assert (await api.post(f"/driver/rides/{nusrat}/arrive", headers=as_user(AS_JASHIM))).status_code == 200
    assert error(await api.post(f"/rides/{nusrat}/cancel", json={}, headers=as_user(AS_NUSRAT))) == \
           (409, "INVALID_TRANSITION")


# ---- driver offers ----------------------------------------------------------------------------------------------

async def offered(db, driver=JASHIM, **kw) -> str:
    r = ride(**kw)
    await add(db, r, RideOffer(ride_id=r.id, driver_id=driver, distance_m=800))
    return r.id


async def test_offers_list(api, db):
    await add(db, shift())
    ride_id = await offered(db)
    resp = await api.get("/driver/offers", headers=as_user(AS_JASHIM))
    assert resp.status_code == 200
    (offer,) = resp.json()
    assert {k: offer[k] for k in ("ride_id", "passenger_name", "pickup_zone", "dropoff_zone", "seats",
                                  "estimated_fare_poysha", "distance_m")} == \
           {"ride_id": ride_id, "passenger_name": "Nusrat", "pickup_zone": "BANANI", "dropoff_zone": "MOHAKHALI",
            "seats": 1, "estimated_fare_poysha": 11000, "distance_m": 800}


async def test_offers_only_open_ones_and_only_mine(api, db):
    await add(db, shift())
    taken = await offered(db, passenger_id="p-taken", passenger_name="T")
    async with db.rw.begin() as s:
        (await s.get(RideRequest, taken)).status = "CANCELLED"   # the ride is gone
    declined = await offered(db, passenger_id="p-declined", passenger_name="D")
    await api.post(f"/driver/offers/{declined}/decline", headers=as_user(AS_JASHIM))
    await offered(db, driver=KARIM, passenger_id="p-karim", passenger_name="K")  # Karim's, not Jashim's
    assert (await api.get("/driver/offers", headers=as_user(AS_JASHIM))).json() == []


async def test_accept_returns_the_new_pool(api, db):
    await add(db, shift())
    ride_id = await offered(db)
    resp = await api.post(f"/driver/offers/{ride_id}/accept", headers=as_user(AS_JASHIM))
    assert resp.status_code == 200
    pool = resp.json()
    assert (pool["status"], pool["vehicle_nickname"], pool["occupied_seats"], pool["max_capacity"]) == \
           ("FORMING", "Bullet", 1, 4)
    assert [(w["seq"], w["kind"], w["zone"], w["passenger_name"], w["done"]) for w in pool["waypoints"]] == \
           [(1, "PICKUP", "BANANI", "Nusrat", False), (2, "DROPOFF", "MOHAKHALI", "Nusrat", False)]
    assert "_member_ids" not in pool and "fare" not in resp.text and "poysha" not in resp.text


async def test_accept_refusals(api, db):
    await add(db, shift(is_online=False))
    ride_id = await offered(db)
    assert error(await api.post(f"/driver/offers/{ride_id}/accept", headers=as_user(AS_JASHIM))) == \
           (409, "DRIVER_OFFLINE")
    assert error(await api.post("/driver/offers/nope/accept", headers=as_user(AS_JASHIM))) == \
           (404, "OFFER_NOT_FOUND")


async def test_decline(api, db):
    await add(db, shift())
    ride_id = await offered(db)
    resp = await api.post(f"/driver/offers/{ride_id}/decline", headers=as_user(AS_JASHIM))
    assert resp.status_code == 204 and resp.content == b""
    assert error(await api.post(f"/driver/offers/{ride_id}/decline", headers=as_user(AS_JASHIM))) == \
           (404, "OFFER_NOT_FOUND")  # declining twice
    async with db.ro() as s:
        assert (await s.get(RideOffer, (ride_id, JASHIM))).status == "DECLINED"
        assert (await s.get(RideRequest, ride_id)).status == "REQUESTED"  # other drivers can still take it


async def test_cannot_decline_after_accepting(api, db):
    await add(db, shift())
    ride_id = await offered(db)
    await api.post(f"/driver/offers/{ride_id}/accept", headers=as_user(AS_JASHIM))
    assert error(await api.post(f"/driver/offers/{ride_id}/decline", headers=as_user(AS_JASHIM))) == \
           (404, "OFFER_NOT_FOUND")


# ---- driver pools -----------------------------------------------------------------------------------------------

async def test_no_live_pool_is_204(api, db):
    await add(db, shift())
    resp = await api.get("/driver/pool", headers=as_user(AS_JASHIM))
    assert resp.status_code == 204 and resp.content == b""


async def test_live_pool_and_past_pools(api, db):
    pool_id, nusrat = await bullet_with_nusrat(db)
    me = as_user(AS_JASHIM)
    assert (await api.get("/driver/pool", headers=me)).json()["id"] == pool_id
    assert (await api.get("/driver/pools", headers=me)).json() == []  # live, not past
    for action in ("arrive", "start", "complete"):
        await api.post(f"/driver/rides/{nusrat}/{action}", headers=me)
    assert (await api.get("/driver/pool", headers=me)).status_code == 204
    past = (await api.get("/driver/pools", headers=me)).json()
    assert [(p["id"], p["status"]) for p in past] == [(pool_id, "COMPLETED")]
    assert past[0]["riders"][0]["status"] == "COMPLETED"


# ---- driver moves a rider through the trip ----------------------------------------------------------------------

async def test_arrive_start_complete(api, db):
    pool_id, nusrat = await bullet_with_nusrat(db)
    me = as_user(AS_JASHIM)
    arrived = (await api.post(f"/driver/rides/{nusrat}/arrive", headers=me)).json()
    assert (arrived["status"], arrived["riders"][0]["status"]) == ("FORMING", "DRIVER_ARRIVED")
    started = (await api.post(f"/driver/rides/{nusrat}/start", headers=me)).json()
    assert (started["status"], started["waypoints"][0]["done"]) == ("IN_PROGRESS", True)
    done = (await api.post(f"/driver/rides/{nusrat}/complete", headers=me)).json()
    assert (done["id"], done["status"], done["occupied_seats"]) == (pool_id, "COMPLETED", 0)


async def test_skipping_a_step_409(api, db):
    _, nusrat = await bullet_with_nusrat(db)
    assert error(await api.post(f"/driver/rides/{nusrat}/start", headers=as_user(AS_JASHIM))) == \
           (409, "INVALID_TRANSITION")


@pytest.mark.parametrize("body", [{}, {"reason": ""}, {"reason": "x" * 201}])
async def test_driver_cancel_needs_a_reason(api, db, body):
    _, nusrat = await bullet_with_nusrat(db)
    resp = await api.post(f"/driver/rides/{nusrat}/cancel", json=body, headers=as_user(AS_JASHIM))
    assert error(resp) == (422, "VALIDATION_ERROR")


async def test_no_show(api, db):
    pool_id, nusrat = await bullet_with_nusrat(db)
    me = as_user(AS_JASHIM)
    await api.post(f"/driver/rides/{nusrat}/arrive", headers=me)
    resp = await api.post(f"/driver/rides/{nusrat}/cancel", json={"reason": "PASSENGER_NO_SHOW"}, headers=me)
    assert resp.status_code == 200 and resp.json()["status"] == "CANCELLED"  # she was his only rider
    detail = (await api.get(f"/rides/{nusrat}", headers=as_user(AS_NUSRAT))).json()
    assert (detail["cancel_reason"], detail["history"][-1]["actor_role"]) == ("PASSENGER_NO_SHOW", "DRIVER")


async def test_passengers_cannot_use_driver_routes(api, db):
    _, nusrat = await bullet_with_nusrat(db)
    for method, path in [("get", "/driver/offers"), ("get", "/driver/pool"),
                         ("post", f"/driver/rides/{nusrat}/arrive")]:
        assert error(await getattr(api, method)(path, headers=as_user(AS_NUSRAT))) == (403, "FORBIDDEN")


# ---- internal ---------------------------------------------------------------------------------------------------

async def test_live_pool_for_identity(api, db):
    await add(db, shift(), shift(KARIM, driver_name="Karim"))
    assert (await api.get(f"/internal/drivers/{JASHIM}/live-pool", headers=GATEWAY)).json() == {"pool_id": None}
    ride_id = await offered(db)
    pool = (await api.post(f"/driver/offers/{ride_id}/accept", headers=as_user(AS_JASHIM))).json()
    assert (await api.get(f"/internal/drivers/{JASHIM}/live-pool", headers=GATEWAY)).json() == \
           {"pool_id": pool["id"]}
    assert (await api.get(f"/internal/drivers/{KARIM}/live-pool", headers=GATEWAY)).json() == {"pool_id": None}
    for action in ("arrive", "start", "complete"):
        await api.post(f"/driver/rides/{ride_id}/{action}", headers=as_user(AS_JASHIM))
    assert (await api.get(f"/internal/drivers/{JASHIM}/live-pool", headers=GATEWAY)).json() == {"pool_id": None}


async def test_internal_needs_the_token(api):
    assert error(await api.get(f"/internal/drivers/{JASHIM}/live-pool")) == (401, "UNAUTHORIZED_INTERNAL")


async def test_request_id_reaches_fare_and_matching(api, fare, matching, monkeypatch):
    seen = []
    real_quote, real_eval = fare.quote_for, matching.evaluate

    async def quote_for(*a):
        seen.append(("fare", a[-1]))
        return await real_quote(*a)

    async def evaluate(payload, request_id):
        seen.append(("matching", request_id))
        return await real_eval(payload, request_id)

    monkeypatch.setattr(fare, "quote_for", quote_for)
    monkeypatch.setattr(matching, "evaluate", evaluate)
    await api.post("/rides", json=TO_MOHAKHALI, headers=as_user(AS_NUSRAT) | {"X-Request-Id": "req-42"})
    assert seen == [("fare", "req-42"), ("matching", "req-42")]


async def test_offers_are_stored_for_the_right_drivers(api, db, matching):
    matching.candidates = [(JASHIM, 800), (KARIM, 1900)]
    body = (await api.post("/rides", json=TO_MOHAKHALI, headers=as_user(AS_NUSRAT))).json()
    async with db.ro() as s:
        rows = (await s.execute(select(RideOffer.driver_id).where(RideOffer.ride_id == body["id"]))).scalars().all()
    assert sorted(rows) == [JASHIM, KARIM]
