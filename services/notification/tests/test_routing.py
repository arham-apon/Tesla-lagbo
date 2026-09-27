"""7.4 / 7.7.2: who hears about what, and what they may see. One test per event type, then privacy checks over a
whole evening, including the plan's "Rafiq never receives a message containing Nusrat's fare"."""
import json
import sqlite3
from datetime import datetime
from unittest.mock import patch

import pytest

from tesla_common.events import emit

from app.routing import recipients
from conftest import JASHIM, NUSRAT, RAFIQ

SHIRIN, KARIM = "44444444-4444-4444-8444-444444444444", "driver-karim"
PASSENGERS = {NUSRAT, RAFIQ, SHIRIN}
POOL, R_N, R_R, R_S = "pool-bullet", "ride-nusrat", "ride-rafiq", "ride-shirin"


class _Capture:
    def __init__(self):
        self.rows = []

    def add(self, row):
        self.rows.append(row)


def event(routing_key: str, data: dict) -> dict:
    """An envelope from the real tesla_common emit(), with a planted field no allowlist names."""
    session = _Capture()
    with patch("tesla_common.events.utcnow", return_value=datetime(2026, 9, 24, 8, 41, 5)):
        emit(session, lambda **kw: kw, "test", routing_key, data | {"secret": "must-never-reach-a-phone"})
    return json.loads(session.rows[0]["payload"])


# The registry's exact payloads (plan 0.4), as Trip and Fare send them (their contract tests guard these).
def requested(ride_id=R_S, passenger=SHIRIN, candidates=(JASHIM, KARIM)):
    return event("trip.ride.requested", {"ride_id": ride_id, "passenger_id": passenger, "pickup_zone": "BANANI",
                                         "dropoff_zone": "GULSHAN_2", "seats": 1, "estimated_fare_poysha": 4500,
                                         "candidate_driver_ids": list(candidates)})


def matched(ride_id, passenger, joined):
    return event("trip.ride.matched", {"ride_id": ride_id, "pool_id": POOL, "passenger_id": passenger,
                                       "driver_id": JASHIM, "driver_name": "Jashim", "vehicle_nickname": "Bullet",
                                       "seats": 1, "joined_existing_pool": joined})


def status(ride_id, passenger, frm, to):
    return event("trip.ride.status_changed", {"ride_id": ride_id, "pool_id": POOL, "passenger_id": passenger,
                                              "driver_id": JASHIM, "from_status": frm, "to_status": to,
                                              "actor_role": "DRIVER"})


def cancelled(ride_id=R_S, passenger=SHIRIN, pool=None, driver=None, by="PASSENGER"):
    return event("trip.ride.cancelled", {"ride_id": ride_id, "pool_id": pool, "passenger_id": passenger,
                                         "driver_id": driver, "from_status": "REQUESTED", "cancelled_by": by,
                                         "reason": "changed_plans", "quote_id": "q-secret"})


def pool_updated(members, status_="FORMING"):
    return event("trip.pool.updated", {"pool_id": POOL, "driver_id": JASHIM, "status": status_, "occupied_seats": 2,
                                       "max_capacity": 4, "member_passenger_ids": list(members),
                                       "waypoints": [{"seq": 1, "kind": "PICKUP", "zone": "BANANI",
                                                      "ride_id": R_N, "done": False}]})


def settled(ride_id, passenger, total, method="WALLET", status_="PAID"):
    return event("fare.ride.settled", {"fare_id": f"fare-{ride_id}", "ride_id": ride_id, "passenger_id": passenger,
                                       "driver_id": JASHIM, "base_poysha": 3000,
                                       "distance_charge_poysha": total - 3000 + (1050 if total == 7200 else 600),
                                       "pool_discount_poysha": 1050 if total == 7200 else 600, "total_poysha": total,
                                       "payment_method": method, "payment_status": status_})


def evening() -> list[dict]:
    """Nusrat and Rafiq in Bullet; Shirin asks for a ride elsewhere and gives up; both fares settle."""
    return [
        matched(R_N, NUSRAT, joined=False), pool_updated([NUSRAT]),
        matched(R_R, RAFIQ, joined=True), pool_updated([NUSRAT, RAFIQ]),
        requested(), cancelled(),
        status(R_N, NUSRAT, "MATCHED", "DRIVER_ARRIVED"), status(R_N, NUSRAT, "DRIVER_ARRIVED", "STARTED"),
        status(R_R, RAFIQ, "MATCHED", "DRIVER_ARRIVED"), status(R_R, RAFIQ, "DRIVER_ARRIVED", "STARTED"),
        pool_updated([NUSRAT, RAFIQ], "IN_PROGRESS"),
        event("trip.ride.completed", {"ride_id": R_R, "passenger_id": RAFIQ, "driver_id": JASHIM}),
        settled(R_R, RAFIQ, 5400, method="CASH"),
        settled(R_N, NUSRAT, 7200, status_="FAILED"),  # her wallet was short: Jashim collects cash
        pool_updated([], "COMPLETED"),
    ]


def everything() -> list[tuple[str, dict]]:
    return [pair for ev in evening() for pair in recipients(ev)]


# ---- one test per event type (plan 7.7.2) ----------------------------------------------------------------------

def test_requested_goes_to_each_candidate_driver_as_an_offer():
    out = recipients(requested())
    assert [(u, m["type"]) for u, m in out] == [(JASHIM, "ride.offer"), (KARIM, "ride.offer")]
    assert out[0][1]["data"] == {"ride_id": R_S, "pickup_zone": "BANANI", "dropoff_zone": "GULSHAN_2", "seats": 1,
                                 "estimated_fare_poysha": 4500}  # no passenger id, name or phone


def test_requested_with_no_driver_nearby_tells_nobody():
    assert recipients(requested(candidates=())) == []


def test_matched_passenger_gets_driver_name_and_bullet():
    (p_user, p_msg), (d_user, d_msg) = recipients(matched(R_R, RAFIQ, joined=True))
    assert (p_user, p_msg["type"]) == (RAFIQ, "ride.matched")
    assert p_msg["data"] == {"ride_id": R_R, "pool_id": POOL, "driver_name": "Jashim", "vehicle_nickname": "Bullet",
                             "seats": 1}
    assert (d_user, d_msg["data"]) == (JASHIM, {"ride_id": R_R, "pool_id": POOL, "seats": 1,
                                                "joined_existing_pool": True})


def test_status_goes_to_passenger_and_driver():
    out = recipients(status(R_N, NUSRAT, "MATCHED", "DRIVER_ARRIVED"))
    assert [(u, m["type"]) for u, m in out] == [(NUSRAT, "ride.status"), (JASHIM, "ride.status")]
    assert out[0][1]["data"] == {"ride_id": R_N, "pool_id": POOL, "from_status": "MATCHED",
                                 "to_status": "DRIVER_ARRIVED", "actor_role": "DRIVER"}


def test_cancelled_before_any_driver_tells_only_the_passenger():
    ((user, msg),) = recipients(cancelled())
    assert (user, msg["type"]) == (SHIRIN, "ride.cancelled")
    assert msg["data"] == {"ride_id": R_S, "pool_id": None, "from_status": "REQUESTED", "cancelled_by": "PASSENGER",
                           "reason": "changed_plans"}  # the quote id stays between Trip and Fare


def test_cancelled_after_matching_tells_the_driver_too():
    out = recipients(cancelled(R_N, NUSRAT, POOL, JASHIM, "DRIVER"))
    assert [u for u, _ in out] == [NUSRAT, JASHIM]


def test_pool_updated_goes_to_the_driver_only():
    ((user, msg),) = recipients(pool_updated([NUSRAT, RAFIQ]))
    assert (user, msg["type"]) == (JASHIM, "pool.updated")
    assert msg["data"]["member_passenger_ids"] == [NUSRAT, RAFIQ] and len(msg["data"]["waypoints"]) == 1


def test_settled_passenger_gets_her_own_fare_in_full():
    (p_user, p_msg), _ = recipients(settled(R_N, NUSRAT, 7200))
    assert p_user == NUSRAT and p_msg["type"] == "fare.settled"
    assert (p_msg["data"]["total_poysha"], p_msg["data"]["pool_discount_poysha"]) == (7200, 1050)


def test_settled_driver_gets_what_he_needs_to_collect_cash():
    _, (d_user, d_msg) = recipients(settled(R_N, NUSRAT, 7200, status_="FAILED"))
    assert (d_user, d_msg["data"]) == (JASHIM, {"ride_id": R_N, "total_poysha": 7200, "payment_method": "WALLET",
                                                "payment_status": "FAILED"})


@pytest.mark.parametrize("routing_key", ["trip.ride.completed", "trip.ride.something_new", "identity.driver.online"])
def test_other_events_tell_nobody(routing_key):
    assert recipients(event(routing_key, {"ride_id": R_N, "passenger_id": NUSRAT, "driver_id": JASHIM})) == []


def test_message_envelope():
    ev = matched(R_N, NUSRAT, joined=False)
    _, msg = recipients(ev)[0]
    assert set(msg) == {"type", "event_id", "at", "data"}
    assert (msg["event_id"], msg["at"]) == (ev["event_id"], "2026-09-24T08:41:05.000000Z")


# ---- privacy over the whole evening ---------------------------------------------------------------------------

def test_rafiq_never_receives_a_message_containing_nusrats_fare():
    """The plan's test (7.7.2)."""
    to_rafiq = json.dumps([m for u, m in everything() if u == RAFIQ])
    assert "7200" not in to_rafiq and "fare-ride-nusrat" not in to_rafiq and R_N not in to_rafiq


def test_each_passenger_only_hears_about_their_own_ride():
    own = {NUSRAT: R_N, RAFIQ: R_R, SHIRIN: R_S}
    for user, msg in everything():
        if user in PASSENGERS:
            assert msg["data"]["ride_id"] == own[user], (user, msg)


def test_passengers_never_get_co_rider_data():
    for user, msg in everything():
        if user in PASSENGERS:
            assert msg["type"] != "pool.updated"
            text = json.dumps(msg)
            assert not (PASSENGERS - {user}) & {p for p in PASSENGERS if p in text}, (user, msg)


def test_no_planted_field_ever_escapes():
    """The allowlist change: a field an event gains later is never forwarded."""
    assert all("secret" not in msg["data"] for _, msg in everything())


def test_nobody_is_ever_messaged_as_an_empty_id():
    assert all(user for user, _ in everything())


def test_everyone_hears_what_they_should():
    got = {}
    for user, msg in everything():
        got.setdefault(user, []).append(msg["type"])
    assert got[NUSRAT] == ["ride.matched", "ride.status", "ride.status", "fare.settled"]
    assert got[RAFIQ] == ["ride.matched", "ride.status", "ride.status", "fare.settled"]
    assert got[SHIRIN] == ["ride.cancelled"]
    assert got[KARIM] == ["ride.offer"]
    assert got[JASHIM].count("pool.updated") == 4 and got[JASHIM].count("fare.settled") == 2


def test_every_message_fits_the_inbox():
    """7.3's inbox refuses payloads that aren't JSON; every routed message must pass SQLite's own check."""
    conn = sqlite3.connect(":memory:")
    for _, msg in everything():
        assert conn.execute("SELECT json_valid(?)", (json.dumps(msg),)).fetchone() == (1,)
