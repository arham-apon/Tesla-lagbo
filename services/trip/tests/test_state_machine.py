"""5.4 / 5.9.2: every (from, to, actor) combination; only the plan's table passes, everything else is 409."""
import re
from itertools import product
from typing import get_args

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from tesla_common.errors import DomainError, install_error_handlers

from app.models import ACTIVE_RIDE_STATUSES, RideRequest, RideStatusHistory
from app.schemas import RideStatus
from app.state_machine import TRANSITIONS, Actor, assert_transition

STATUSES = get_args(RideStatus)
ACTORS = get_args(Actor)

# The plan's 5.4 table, written out by hand (not copied from TRANSITIONS) so a change to either one is noticed.
ALLOWED = {
    ("REQUESTED", "MATCHED", "SYSTEM"),             # auto-join
    ("REQUESTED", "MATCHED", "DRIVER"),             # driver accepts an offer
    ("REQUESTED", "CANCELLED", "PASSENGER"),
    ("REQUESTED", "CANCELLED", "SYSTEM"),           # NO_DRIVER_FOUND (the sweeper)
    ("MATCHED", "DRIVER_ARRIVED", "DRIVER"),
    ("MATCHED", "CANCELLED", "PASSENGER"),
    ("MATCHED", "CANCELLED", "DRIVER"),
    ("DRIVER_ARRIVED", "STARTED", "DRIVER"),
    ("DRIVER_ARRIVED", "CANCELLED", "DRIVER"),      # PASSENGER_NO_SHOW
    ("STARTED", "COMPLETED", "DRIVER"),
}
EVERY = list(product(STATUSES, STATUSES, ACTORS))  # 6 x 6 x 3 = 108


def test_the_grid_is_complete():
    assert len(EVERY) == 108 and ALLOWED <= set(EVERY)


@pytest.mark.parametrize("current, target, actor", EVERY)
def test_transition(current, target, actor):
    if (current, target, actor) in ALLOWED:
        assert_transition(current, target, actor)
    else:
        with pytest.raises(DomainError) as e:
            assert_transition(current, target, actor)
        assert (e.value.code, e.value.status) == ("INVALID_TRANSITION", 409)


def test_seven_status_pairs():
    assert len(TRANSITIONS) == 7 and {(c, t) for c, t, _ in ALLOWED} == set(TRANSITIONS)


# ---- the rules the story depends on --------------------------------------------------------------------------

@pytest.mark.parametrize("terminal", ["COMPLETED", "CANCELLED"])
def test_finished_rides_never_change(terminal):
    assert not [k for k in TRANSITIONS if k[0] == terminal]


def test_active_statuses_are_exactly_the_ones_that_can_still_move():
    # models.ACTIVE_RIDE_STATUSES drives the "one active ride per passenger" index and pool completion.
    assert set(ACTIVE_RIDE_STATUSES) == {c for c, _ in TRANSITIONS}


def test_passenger_cannot_cancel_once_driver_has_arrived():
    # Plan 5.9 step 5. Nusrat can't walk away while Jashim is waiting; only he can cancel (no-show).
    with pytest.raises(DomainError):
        assert_transition("DRIVER_ARRIVED", "CANCELLED", "PASSENGER")


def test_nobody_cancels_a_started_ride():
    for actor in ACTORS:
        with pytest.raises(DomainError):
            assert_transition("STARTED", "CANCELLED", actor)


def test_only_the_driver_moves_the_ride_forward():
    forward = [(c, t) for c, t in TRANSITIONS if t not in ("CANCELLED", "MATCHED")]
    assert forward and all(TRANSITIONS[k] == {"DRIVER"} for k in forward)


def test_no_skipping_steps():
    for current, target in [("MATCHED", "STARTED"), ("MATCHED", "COMPLETED"), ("DRIVER_ARRIVED", "COMPLETED"),
                            ("REQUESTED", "STARTED")]:
        with pytest.raises(DomainError):
            assert_transition(current, target, "DRIVER")


def test_repeating_a_move_is_refused():
    # A second "cancel" or "arrive" is not silently accepted: the ride is already there.
    for status in STATUSES:
        with pytest.raises(DomainError):
            assert_transition(status, status, "DRIVER")


def test_unknown_status_or_actor_refused():
    with pytest.raises(DomainError):
        assert_transition("REQUESTED", "FLYING", "PASSENGER")
    with pytest.raises(DomainError):
        assert_transition("REQUESTED", "CANCELLED", "ADMIN")


def test_every_status_is_reachable_from_requested():
    seen, todo = {"REQUESTED"}, ["REQUESTED"]
    while todo:
        cur = todo.pop()
        for c, t in TRANSITIONS:
            if c == cur and t not in seen:
                seen.add(t)
                todo.append(t)
    assert seen == set(STATUSES)


# ---- agrees with the database (5.3) ---------------------------------------------------------------------------

def _check_values(table, name) -> set[str]:
    ck = next(c for c in table.constraints if c.name == name)
    return set(re.findall(r"'([A-Z_]+)'", str(ck.sqltext)))


def test_statuses_match_the_database_rule():
    assert _check_values(RideRequest.__table__, "ck_ride_status") == set(STATUSES)


def test_actors_match_the_audit_rule():
    assert _check_values(RideStatusHistory.__table__, "ck_history_actor") == set(ACTORS)


# ---- what the phone sees --------------------------------------------------------------------------------------

def test_refusal_reaches_the_client_as_409():
    app = FastAPI()
    install_error_handlers(app)

    @app.post("/cancel")
    async def cancel():
        assert_transition("DRIVER_ARRIVED", "CANCELLED", "PASSENGER")

    resp = TestClient(app).post("/cancel")
    assert resp.status_code == 409
    err = resp.json()["error"]
    assert err["code"] == "INVALID_TRANSITION"
    assert err["message"] == "DRIVER_ARRIVED → CANCELLED is not allowed for PASSENGER"
