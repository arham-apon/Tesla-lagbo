"""4.3: request shapes: Dhaka bounding box for pings, seat and candidate limits for evaluate."""
import pytest
from pydantic import ValidationError

from app.schemas import NEW_RIDE, EvaluateIn, LocationPing, OpenPool, Stop


@pytest.mark.parametrize("lat, lng", [(23.7937, 90.4066), (23.60, 90.30), (23.95, 90.55)])
def test_ping_inside_dhaka(lat, lng):
    assert LocationPing(lat=lat, lng=lng).lat == lat


@pytest.mark.parametrize("lat, lng", [(22.3569, 91.7832),   # Chattogram
                                      (0.0, 0.0),           # a phone with no GPS fix
                                      (23.59, 90.40), (23.96, 90.40), (23.79, 90.29), (23.79, 90.56)])
def test_ping_outside_dhaka_rejected(lat, lng):
    with pytest.raises(ValidationError):
        LocationPing(lat=lat, lng=lng)


@pytest.mark.parametrize("heading, ok", [(None, True), (0, True), (359, True), (360, False), (-1, False)])
def test_heading(heading, ok):
    if ok:
        LocationPing(lat=23.79, lng=90.40, heading=heading)
    else:
        with pytest.raises(ValidationError):
            LocationPing(lat=23.79, lng=90.40, heading=heading)


@pytest.mark.parametrize("seats, ok", [(0, False), (1, True), (6, True), (7, False)])
def test_evaluate_seats(seats, ok):
    body = {"pickup_zone": "BANANI", "dropoff_zone": "MOHAKHALI", "seats": seats}
    if ok:
        assert EvaluateIn(**body).seats == seats
    else:
        with pytest.raises(ValidationError):
            EvaluateIn(**body)


def test_evaluate_defaults():
    req = EvaluateIn(pickup_zone="BANANI", dropoff_zone="GULSHAN_1", seats=1)
    assert req.open_pools == [] and req.max_candidates == 5


@pytest.mark.parametrize("n", [0, 21])
def test_max_candidates_bounds(n):
    with pytest.raises(ValidationError):
        EvaluateIn(pickup_zone="BANANI", dropoff_zone="GULSHAN_1", seats=1, max_candidates=n)


def test_open_pool_snapshot_from_trip():
    pool = OpenPool(pool_id="p1", driver_id="jashim", pickup_zone="BANANI", remaining_seats=2, version=3,
                    stops=[{"ride_id": "nusrat", "kind": "PICKUP", "zone": "BANANI"},
                           {"ride_id": "nusrat", "kind": "DROPOFF", "zone": "MOHAKHALI"}])
    assert pool.stops[1] == Stop(ride_id="nusrat", kind="DROPOFF", zone="MOHAKHALI", done=False)


def test_negative_remaining_seats_rejected():
    with pytest.raises(ValidationError):
        OpenPool(pool_id="p1", driver_id="d", pickup_zone="BANANI", remaining_seats=-1, version=1, stops=[])


def test_stop_kind_limited():
    with pytest.raises(ValidationError):
        Stop(ride_id="r", kind="WAYPOINT", zone="BANANI")


def test_new_ride_marker_cannot_clash_with_a_uuid():
    assert NEW_RIDE == "__new__"
