"""7.5: GET /notifications?after_id=&limit= (catch-up) and POST /notifications/{id}/read, through the gateway."""
import json

import pytest

from app.models import Notification
from conftest import GATEWAY, JASHIM, NUSRAT, RAFIQ, add, as_user


def note(user_id, n, type_="ride.status") -> Notification:
    return Notification(user_id=user_id, type=type_,
                        payload=json.dumps({"type": type_, "event_id": f"e-{n}", "data": {"n": n}}))


@pytest.fixture
async def inboxes(db):
    """Nusrat's messages 1, 3, 5, 7; Rafiq's 2, 4, 6 (interleaved, as they'd really arrive)."""
    await add(db, *[note(NUSRAT if i % 2 else RAFIQ, i) for i in range(1, 8)])


async def test_catch_up_from_the_start(api, inboxes):
    resp = await api.get("/notifications", headers=as_user(NUSRAT))
    assert resp.status_code == 200
    rows = resp.json()
    assert [r["id"] for r in rows] == [1, 3, 5, 7]
    assert rows[0]["payload"] == {"type": "ride.status", "event_id": "e-1", "data": {"n": 1}}  # JSON, not a string
    assert (rows[0]["type"], rows[0]["read_at"]) == ("ride.status", None)


async def test_catch_up_after_the_last_one_seen(api, inboxes):
    assert [r["id"] for r in (await api.get("/notifications?after_id=3", headers=as_user(NUSRAT))).json()] == [5, 7]
    assert (await api.get("/notifications?after_id=7", headers=as_user(NUSRAT))).json() == []


async def test_paging(api, inboxes):
    me = as_user(NUSRAT)
    page1 = (await api.get("/notifications?limit=2", headers=me)).json()
    page2 = (await api.get(f"/notifications?limit=2&after_id={page1[-1]['id']}", headers=me)).json()
    assert [r["id"] for r in page1 + page2] == [1, 3, 5, 7]


async def test_inboxes_are_private(api, inboxes):
    assert [r["id"] for r in (await api.get("/notifications", headers=as_user(RAFIQ))).json()] == [2, 4, 6]
    assert (await api.get("/notifications", headers=as_user(JASHIM, "DRIVER"))).json() == []


@pytest.mark.parametrize("query", ["after_id=-1", "limit=0", "limit=101", "after_id=abc"])
async def test_bad_query_422(api, query):
    assert (await api.get(f"/notifications?{query}", headers=as_user(NUSRAT))).status_code == 422


async def test_needs_the_gateway_and_a_user(api):
    assert (await api.get("/notifications", headers={"X-User-Id": NUSRAT, "X-User-Role": "PASSENGER"})).status_code \
           == 401  # no internal token: not through the gateway
    assert (await api.get("/notifications", headers=GATEWAY)).status_code == 401  # no user


async def test_mark_read(api, db, inboxes):
    resp = await api.post("/notifications/3/read", headers=as_user(NUSRAT))
    assert resp.status_code == 204 and resp.content == b""
    rows = {r["id"]: r for r in (await api.get("/notifications", headers=as_user(NUSRAT))).json()}
    assert rows[3]["read_at"] is not None and rows[1]["read_at"] is None


async def test_reading_twice_keeps_the_first_time(api, db, inboxes):
    await api.post("/notifications/3/read", headers=as_user(NUSRAT))
    first = (await api.get("/notifications?after_id=2&limit=1", headers=as_user(NUSRAT))).json()[0]["read_at"]
    assert (await api.post("/notifications/3/read", headers=as_user(NUSRAT))).status_code == 204
    again = (await api.get("/notifications?after_id=2&limit=1", headers=as_user(NUSRAT))).json()[0]["read_at"]
    assert again == first


async def test_cannot_mark_someone_elses_message(api, db, inboxes):
    resp = await api.post("/notifications/2/read", headers=as_user(NUSRAT))  # 2 is Rafiq's
    assert (resp.status_code, resp.json()["error"]["code"]) == (404, "NOTIFICATION_NOT_FOUND")
    assert (await api.get("/notifications", headers=as_user(RAFIQ))).json()[0]["read_at"] is None
    missing = await api.post("/notifications/999/read", headers=as_user(NUSRAT))
    assert missing.json()["error"] == resp.json()["error"] | {"request_id": None}  # same answer: ids can't be probed
