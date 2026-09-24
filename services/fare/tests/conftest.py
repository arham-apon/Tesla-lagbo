import os
from pathlib import Path

import pytest

SERVICE_DIR = Path(__file__).resolve().parents[1]
INTERNAL_TOKEN = "test-internal-token"

# app.config builds Settings() at import time, so the environment must be set first.
os.environ.update(REDIS_URL="redis://unused", RABBITMQ_URL="amqp://unused", INTERNAL_TOKEN=INTERNAL_TOKEN,
                  DB_PATH="unused.db")

import fakeredis  # noqa: E402
from alembic import command  # noqa: E402
from alembic.config import Config  # noqa: E402

from tesla_common.db import Database  # noqa: E402

# Identity's fixed seed ids (Part 3), so Fare's data lines up with the rest of the demo.
JASHIM = "11111111-1111-4111-8111-111111111111"
NUSRAT = "22222222-2222-4222-8222-222222222222"
RAFIQ = "33333333-3333-4333-8333-333333333333"


def alembic_config(db_path: Path) -> Config:
    cfg = Config(str(SERVICE_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(SERVICE_DIR / "migrations"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite+aiosqlite:///{db_path.as_posix()}")
    return cfg


@pytest.fixture
def db_path(tmp_path) -> Path:
    """A fresh fare.db, migrated to head (tables + tariff v1) with the real Alembic migrations."""
    path = tmp_path / "fare.db"
    command.upgrade(alembic_config(path), "head")
    return path


@pytest.fixture
async def db(db_path):
    database = Database(str(db_path))
    yield database
    await database.dispose()


@pytest.fixture
async def redis():
    r = fakeredis.aioredis.FakeRedis(decode_responses=True)
    yield r
    await r.aclose()


async def add(db: Database, *rows) -> None:
    async with db.rw.begin() as s:
        for r in rows:
            s.add(r)
            await s.flush()  # insert in the given order, so parents exist before children
