"""3.3: the database itself refuses bad data (constraints), independent of any API code."""
import json

import pytest
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError

from tesla_common.events import emit

from app.models import Driver, Outbox, User, Vehicle

JASHIM = "11111111-1111-4111-8111-111111111111"
BULLET = "b1111111-1111-4111-8111-111111111111"


def user(uid: str, name: str, phone: str, role: str = "PASSENGER") -> User:
    return User(id=uid, full_name=name, phone=phone, password_hash="argon2-hash", role=role)


def bullet(**overrides) -> Vehicle:
    fields = dict(id=BULLET, driver_id=JASHIM, nickname="Bullet", make="Tesla", model="Model Y",
                  plate="DHAKA-TESLA-11", seat_capacity=3)
    return Vehicle(**(fields | overrides))


@pytest.fixture
async def jashim(db):
    async with db.rw.begin() as s:
        s.add(user(JASHIM, "Jashim", "01711000001", "DRIVER"))
        s.add(Driver(user_id=JASHIM, license_number="DK-0001"))
        s.add(bullet())


async def insert_fails(db, *rows) -> None:
    with pytest.raises(IntegrityError):
        async with db.rw.begin() as s:
            s.add_all(rows)


async def test_register_style_insert_user_and_driver_together(db):
    # Plan 3.4 register inserts the users row and the drivers row in one transaction.
    async with db.rw.begin() as s:
        s.add(Driver(user_id="u-k", license_number="DK-0009"))  # added first on purpose
        s.add(user("u-k", "Karim", "01711000005", "DRIVER"))
    async with db.ro() as s:
        assert (await s.get(Driver, "u-k")).license_number == "DK-0009"


async def test_driver_with_vehicle_round_trip(db, jashim):
    async with db.ro() as s:
        driver = (await s.execute(select(Driver).where(Driver.user_id == JASHIM))).scalar_one()
    assert driver.status == "OFFLINE"              # default
    assert driver.vehicle.nickname == "Bullet"     # loaded eagerly (lazy="selectin"), safe after the session closed
    assert driver.vehicle.seat_capacity == 3


async def test_phone_is_unique(db, jashim):
    await insert_fails(db, user("u-dup", "Fake Jashim", "01711000001"))


async def test_role_must_be_known(db):
    await insert_fails(db, user("u-x", "Mallory", "01711000009", role="SUPERUSER"))


@pytest.mark.parametrize("seats", [0, 7, -1])
async def test_seat_capacity_between_1_and_6(db, jashim, seats):
    async with db.rw.begin() as s:
        s.add(user("u-2", "Karim", "01711000005", "DRIVER"))
        s.add(Driver(user_id="u-2", license_number="DK-0002"))
    await insert_fails(db, bullet(id="v-2", driver_id="u-2", plate="DHAKA-2", seat_capacity=seats))


async def test_six_seats_is_allowed(db, jashim):
    async with db.rw.begin() as s:
        s.add(user("u-2", "Karim", "01711000005", "DRIVER"))
        s.add(Driver(user_id="u-2", license_number="DK-0002"))
        s.add(bullet(id="v-2", driver_id="u-2", plate="DHAKA-2", seat_capacity=6))


async def test_one_vehicle_per_driver(db, jashim):
    await insert_fails(db, bullet(id="v-2", plate="DHAKA-2"))


async def test_plate_is_unique(db, jashim):
    async with db.rw.begin() as s:
        s.add(user("u-2", "Karim", "01711000005", "DRIVER"))
        s.add(Driver(user_id="u-2", license_number="DK-0002"))
    await insert_fails(db, bullet(id="v-2", driver_id="u-2"))


async def test_license_is_unique(db, jashim):
    await insert_fails(db, user("u-2", "Karim", "01711000005", "DRIVER"), Driver(user_id="u-2", license_number="DK-0001"))


async def test_vehicle_needs_a_real_driver(db):
    # Only fails because tesla_common turns on PRAGMA foreign_keys (SQLite ignores FKs otherwise).
    await insert_fails(db, bullet(driver_id="nobody"))


async def test_driver_status_only_online_or_offline(db, jashim):
    with pytest.raises(IntegrityError):
        async with db.rw.begin() as s:
            await s.execute(update(Driver).where(Driver.user_id == JASHIM).values(status="BUSY"))


async def test_cannot_delete_user_who_is_a_driver(db, jashim):
    with pytest.raises(IntegrityError):
        async with db.rw.begin() as s:
            await s.delete(await s.get(User, JASHIM))


async def test_updated_at_moves_on_change(db, jashim):
    async with db.ro() as s:
        before = (await s.get(Driver, JASHIM)).updated_at
    async with db.rw.begin() as s:
        (await s.get(Driver, JASHIM)).status = "ONLINE"
    async with db.ro() as s:
        after = await s.get(Driver, JASHIM)
    assert after.status == "ONLINE" and after.updated_at > before


async def test_outbox_row_written_in_same_transaction(db, jashim):
    async with db.rw.begin() as s:
        (await s.get(Driver, JASHIM)).status = "ONLINE"
        emit(s, Outbox, "identity-service", "identity.driver.online", {"driver_id": JASHIM, "seat_capacity": 3})
    async with db.ro() as s:
        row = (await s.execute(select(Outbox))).scalar_one()
    envelope = json.loads(row.payload)
    assert row.routing_key == "identity.driver.online" and row.published_at is None
    assert envelope["producer"] == "identity-service" and envelope["data"]["seat_capacity"] == 3


async def test_outbox_rolls_back_with_the_change(db, jashim):
    with pytest.raises(RuntimeError):
        async with db.rw.begin() as s:
            (await s.get(Driver, JASHIM)).status = "ONLINE"
            emit(s, Outbox, "identity-service", "identity.driver.online", {"driver_id": JASHIM})
            raise RuntimeError("crash before commit")
    async with db.ro() as s:
        assert (await s.execute(select(Outbox))).first() is None
        assert (await s.get(Driver, JASHIM)).status == "OFFLINE"
