"""Idempotent demo data: python -m app.seed (runs on every container start; skips anyone already there)."""
import asyncio
import logging

from argon2 import PasswordHasher
from sqlalchemy import select

from tesla_common.db import Database
from tesla_common.logging import configure_logging

from .config import settings
from .deps import db
from .models import Driver, User, Vehicle

log = logging.getLogger("identity.seed")
PASSWORD = "Pool@1234"
JASHIM = "11111111-1111-4111-8111-111111111111"

# Fixed ids so the other services' seed data lines up (plan 3.6 step 7).
USERS = [
    (JASHIM, "Jashim", "01711000001", "DRIVER"),
    ("22222222-2222-4222-8222-222222222222", "Nusrat", "01711000002", "PASSENGER"),
    ("33333333-3333-4333-8333-333333333333", "Rafiq", "01711000003", "PASSENGER"),
    ("44444444-4444-4444-8444-444444444444", "Shirin", "01711000004", "PASSENGER"),
]
JASHIM_LICENSE = "DK-0001"
BULLET = dict(id="b1111111-1111-4111-8111-111111111111", nickname="Bullet", make="Tesla", model="Model 3",
              plate="DHAKA-TESLA-11", seat_capacity=3)


async def seed(database: Database = db) -> list[str]:
    ph = PasswordHasher()
    created: list[str] = []
    async with database.rw.begin() as s:
        for user_id, name, phone, role in USERS:
            if await s.scalar(select(User.id).where(User.phone == phone)):
                continue
            s.add(User(id=user_id, full_name=name, phone=phone, password_hash=ph.hash(PASSWORD), role=role))
            if role == "DRIVER":
                s.add(Driver(user_id=user_id, license_number=JASHIM_LICENSE))
                s.add(Vehicle(driver_id=user_id, **BULLET))
            created.append(name)
    return created


async def main() -> None:
    configure_logging("identity", settings.LOG_LEVEL)
    created = await seed()
    log.info("seed done: %s", ", ".join(created) if created else "nothing new")
    await db.dispose()


if __name__ == "__main__":
    asyncio.run(main())
