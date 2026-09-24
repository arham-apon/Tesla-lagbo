import os
from pathlib import Path

import pytest

SERVICE_DIR = Path(__file__).resolve().parents[1]
INTERNAL_TOKEN = "test-internal-token"

# app.config builds Settings() at import time, so the environment must be set first.
os.environ.update(RABBITMQ_URL="amqp://unused", INTERNAL_TOKEN=INTERNAL_TOKEN, DB_PATH="unused.db")

from alembic import command  # noqa: E402
from alembic.config import Config  # noqa: E402

from tesla_common.db import Database  # noqa: E402
from tesla_common.timeutil import new_id  # noqa: E402

from app.models import DriverShift, Pool, RideRequest  # noqa: E402

JASHIM, KARIM = "driver-jashim", "driver-karim"
NUSRAT, RAFIQ = "passenger-nusrat", "passenger-rafiq"


def alembic_config(db_path: Path) -> Config:
    cfg = Config(str(SERVICE_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(SERVICE_DIR / "migrations"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite+aiosqlite:///{db_path.as_posix()}")
    return cfg


@pytest.fixture
def db_path(tmp_path) -> Path:
    """A fresh trip.db, migrated to head with the real Alembic migrations."""
    path = tmp_path / "trip.db"
    command.upgrade(alembic_config(path), "head")
    return path


@pytest.fixture
async def db(db_path):
    database = Database(str(db_path))
    yield database
    await database.dispose()


def shift(driver_id: str = JASHIM, **kw) -> DriverShift:
    return DriverShift(**{"driver_id": driver_id, "driver_name": "Jashim", "vehicle_id": f"vehicle-{driver_id}",
                          "vehicle_nickname": "Bullet", "seat_capacity": 4, "is_online": True} | kw)


# Ids are set up front (the model default only fills them in at insert), so children can point at them.
def pool(driver_id: str = JASHIM, **kw) -> Pool:
    return Pool(**{"id": new_id(), "driver_id": driver_id, "vehicle_id": f"vehicle-{driver_id}",
                   "vehicle_nickname": "Bullet", "driver_name": "Jashim", "max_capacity": 4,
                   "pickup_zone": "BANANI"} | kw)


def ride(passenger_id: str = NUSRAT, **kw) -> RideRequest:
    return RideRequest(**{"id": new_id(), "passenger_id": passenger_id, "passenger_name": "Nusrat", "seats": 1,
                          "pickup_zone": "BANANI", "dropoff_zone": "MOHAKHALI", "payment_method": "CASH",
                          "quote_id": "quote-1", "solo_distance_m": 3500, "estimated_fare_poysha": 11000,
                          "estimated_pooled_fare_poysha": 8800} | kw)


async def add(db: Database, *rows) -> None:
    async with db.rw.begin() as s:
        for r in rows:
            s.add(r)
            await s.flush()  # insert in the given order, so parents exist before children
