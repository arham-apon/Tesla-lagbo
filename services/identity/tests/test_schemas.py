"""3.3: request/response shapes (pydantic) reject bad input before it reaches the database."""
import pytest
from pydantic import ValidationError

from app.models import Driver, User, Vehicle
from app.schemas import DriverOut, LoginIn, RegisterIn, UserOut, VehicleIn, VehicleOut

NUSRAT = {"full_name": "Nusrat", "phone": "01711000002", "password": "Pool@1234", "role": "PASSENGER"}


@pytest.mark.parametrize("phone", ["01711000002", "01311111111", "01999999999"])
def test_valid_bangladeshi_mobile(phone):
    assert RegisterIn(**NUSRAT | {"phone": phone}).phone == phone
    assert LoginIn(phone=phone, password="x").phone == phone


@pytest.mark.parametrize("phone", ["0171100000", "017110000022", "01211000002", "02711000002",
                                   "+8801711000002", "0171100000a", ""])
def test_invalid_phone_rejected(phone):
    with pytest.raises(ValidationError):
        RegisterIn(**NUSRAT | {"phone": phone})
    with pytest.raises(ValidationError):
        LoginIn(phone=phone, password="x")


def test_password_at_least_8():
    with pytest.raises(ValidationError):
        RegisterIn(**NUSRAT | {"password": "short"})


@pytest.mark.parametrize("role", ["ADMIN", "passenger", "HACKER"])
def test_cannot_self_register_as_admin_or_unknown_role(role):
    with pytest.raises(ValidationError):
        RegisterIn(**NUSRAT | {"role": role})


@pytest.mark.parametrize("seats, ok", [(0, False), (1, True), (3, True), (6, True), (7, False)])
def test_vehicle_seats(seats, ok):
    body = {"nickname": "Bullet", "make": "Tesla", "model": "Model Y", "plate": "DHAKA-TESLA-11", "seat_capacity": seats}
    if ok:
        assert VehicleIn(**body).seat_capacity == seats
    else:
        with pytest.raises(ValidationError):
            VehicleIn(**body)


def test_user_out_never_exposes_password_hash():
    u = User(id="u1", full_name="Nusrat", phone="01711000002", password_hash="secret-hash", role="PASSENGER")
    dumped = UserOut.model_validate(u).model_dump()
    assert dumped == {"id": "u1", "full_name": "Nusrat", "phone": "01711000002", "role": "PASSENGER"}


def test_driver_out_from_orm_objects():
    u = User(id="j", full_name="Jashim", phone="01711000001", password_hash="h", role="DRIVER")
    v = Vehicle(id="b", driver_id="j", nickname="Bullet", make="Tesla", model="Model Y",
                plate="DHAKA-TESLA-11", seat_capacity=3)
    d = Driver(user_id="j", license_number="DK-0001", status="OFFLINE")
    out = DriverOut(user=UserOut.model_validate(u), license_number=d.license_number, status=d.status,
                    vehicle=VehicleOut.model_validate(v))
    assert out.vehicle.seat_capacity == 3 and out.user.full_name == "Jashim"
