import os
from pathlib import Path

import pytest

SERVICE_DIR = Path(__file__).resolve().parents[1]
INTERNAL_TOKEN = "test-internal-token"

# app.config builds Settings() at import time, so the environment must be set first.
os.environ.update(REDIS_URL="redis://unused", RABBITMQ_URL="amqp://unused", INTERNAL_TOKEN=INTERNAL_TOKEN,
                  DB_PATH="unused.db")

from alembic import command  # noqa: E402
from alembic.config import Config  # noqa: E402

from tesla_common.db import Database  # noqa: E402


def alembic_config(db_path: Path) -> Config:
    cfg = Config(str(SERVICE_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(SERVICE_DIR / "migrations"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite+aiosqlite:///{db_path.as_posix()}")
    return cfg


@pytest.fixture
def db_path(tmp_path) -> Path:
    """A fresh matching.db, migrated to head (tables + seeded zones) with the real Alembic migrations."""
    path = tmp_path / "matching.db"
    command.upgrade(alembic_config(path), "head")
    return path


@pytest.fixture
async def db(db_path):
    database = Database(str(db_path))
    yield database
    await database.dispose()


@pytest.fixture
async def redis():
    import fakeredis
    r = fakeredis.aioredis.FakeRedis(decode_responses=True)
    yield r
    await r.aclose()


@pytest.fixture
async def dist(db):
    """The real DistanceTable, loaded from the migrated database (9 zones + the plan's overrides)."""
    from app.geo import load_distance_table
    async with db.ro() as s:
        return await load_distance_table(s)


@pytest.fixture
async def api(db, dist, redis):
    """The three routers on a test app with app.state filled the way the lifespan will (plan 4.8 step 5)."""
    import httpx
    from fastapi import FastAPI

    from tesla_common.errors import install_error_handlers

    from app.geo import load_zones
    from app.routers import driver, internal, public

    app = FastAPI()
    install_error_handlers(app)
    for module in (public, driver, internal):
        app.include_router(module.router)
    async with db.ro() as s:
        app.state.zones = await load_zones(s)
    app.state.dist, app.state.redis = dist, redis
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://matching") as c:
        yield c


GATEWAY = {"X-Internal-Token": INTERNAL_TOKEN}


def as_user(user_id: str, role: str = "DRIVER") -> dict:
    return GATEWAY | {"X-User-Id": user_id, "X-User-Role": role, "X-User-Name": user_id.title()}
