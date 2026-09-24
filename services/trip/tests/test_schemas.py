"""5.3: request shapes refuse bad input at the door; response shapes read straight from database rows."""
import pytest
from pydantic import ValidationError

from app.models import RideRequest, RideStatusHistory
from app.schemas import CancelIn, DriverBrief, DriverCancelIn, PoolOut, RideCreate, RideDetailOut, RideOut
from conftest import NUSRAT, add, ride

BANANI_TO_MOHAKHALI = {"pickup_zone": "BANANI", "dropoff_zone": "MOHAKHALI", "seats": 1}


# ---- RideCreate -------------------------------------------------------------------------------------------------

def test_ride_create_defaults():
    body = RideCreate(**BANANI_TO_MOHAKHALI)
    assert (body.payment_method, body.quote_id) == ("CASH", None)


def test_ride_create_with_quote_and_wallet():
    body = RideCreate(**BANANI_TO_MOHAKHALI, payment_method="WALLET", quote_id="quote-1")
    assert (body.payment_method, body.quote_id) == ("WALLET", "quote-1")


@pytest.mark.parametrize("seats, ok", [(0, False), (1, True), (6, True), (7, False)])
def test_ride_create_seats(seats, ok):
    if ok:
        assert RideCreate(**BANANI_TO_MOHAKHALI | {"seats": seats}).seats == seats
    else:
        with pytest.raises(ValidationError):
            RideCreate(**BANANI_TO_MOHAKHALI | {"seats": seats})


def test_pickup_and_dropoff_must_differ():
    with pytest.raises(ValidationError, match="must differ"):
        RideCreate(pickup_zone="BANANI", dropoff_zone="BANANI", seats=1)


@pytest.mark.parametrize("field", ["pickup_zone", "dropoff_zone"])
@pytest.mark.parametrize("zone", ["banani", "B", "GULSHAN 1", "X" * 31, ""])
def test_zone_code_shape(field, zone):
    # Only the shape is checked here. Whether the zone exists is Matching's/Fare's answer (422 UNKNOWN_ZONE).
    with pytest.raises(ValidationError):
        RideCreate(**BANANI_TO_MOHAKHALI | {field: zone})


def test_unknown_but_well_formed_zone_passes_the_door():
    assert RideCreate(pickup_zone="BANANI", dropoff_zone="MOTIJHEEL", seats=1).dropoff_zone == "MOTIJHEEL"


@pytest.mark.parametrize("method", ["BKASH", "cash", ""])
def test_payment_method(method):
    with pytest.raises(ValidationError):
        RideCreate(**BANANI_TO_MOHAKHALI, payment_method=method)


# ---- CancelIn ---------------------------------------------------------------------------------------------------

def test_cancel_reason():
    assert CancelIn().reason == "changed_plans"
    assert CancelIn(reason="PASSENGER_NO_SHOW").reason == "PASSENGER_NO_SHOW"
    with pytest.raises(ValidationError):
        CancelIn(reason="x" * 201)  # the column is 200 characters


# ---- responses, built from database rows ------------------------------------------------------------------------

async def test_ride_out_from_a_database_row(db):
    r = ride()
    await add(db, r)
    async with db.ro() as s:
        out = RideOut.model_validate(await s.get(RideRequest, r.id))
    assert (out.status, out.pool_id, out.driver, out.estimated_fare_poysha) == ("REQUESTED", None, None, 11000)
    assert out.created_at is not None and out.updated_at is not None


async def test_ride_detail_with_history_rows(db):
    """The exact construction 5.6 uses: RideDetailOut(**out.model_dump(), history=<ORM rows>)."""
    r = ride()
    await add(db, r, RideStatusHistory(ride_id=r.id, to_status="REQUESTED", actor_id=NUSRAT, actor_role="PASSENGER"))
    async with db.ro() as s:
        out = RideOut.model_validate(await s.get(RideRequest, r.id))
        out.driver = DriverBrief(driver_id="driver-jashim", driver_name="Jashim", vehicle_nickname="Bullet")
        rows = [await s.get(RideStatusHistory, 1)]
    detail = RideDetailOut(**out.model_dump(), history=rows)
    assert detail.driver.vehicle_nickname == "Bullet"
    assert [(h.from_status, h.to_status, h.actor_role) for h in detail.history] == [(None, "REQUESTED", "PASSENGER")]


def test_history_does_not_leak_who_did_it():
    # A passenger sees *that* the driver cancelled, not the driver's user id.
    assert "actor_id" not in RideDetailOut.model_fields["history"].annotation.__args__[0].model_fields


def test_driver_view_has_no_fares():
    # PRD: passengers see only their own fare; the driver sees names and stops, never fares.
    fields = set(PoolOut.model_fields) | set(PoolOut.model_fields["riders"].annotation.__args__[0].model_fields)
    assert not {f for f in fields if "fare" in f or "poysha" in f}


def test_driver_cancel_needs_a_reason():
    # 5.6: the driver's reason is required (no default), so the audit always says why a rider was dropped.
    assert DriverCancelIn(reason="PASSENGER_NO_SHOW").reason == "PASSENGER_NO_SHOW"
    for bad in ({}, {"reason": ""}, {"reason": "x" * 201}):
        with pytest.raises(ValidationError):
            DriverCancelIn(**bad)
