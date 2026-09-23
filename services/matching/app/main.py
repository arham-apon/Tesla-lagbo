from contextlib import asynccontextmanager

from fastapi import FastAPI
from redis.asyncio import Redis
from sqlalchemy import text

from tesla_common.errors import install_error_handlers
from tesla_common.health import health_router
from tesla_common.logging import configure_logging

from . import consumers
from .config import settings
from .deps import bus, db
from .geo import load_distance_table, load_zones
from .routers import driver, internal, public


@asynccontextmanager
async def lifespan(app: FastAPI):
    configure_logging("matching", settings.LOG_LEVEL)
    async with db.ro() as s:
        app.state.dist = await load_distance_table(s)
        app.state.zones = await load_zones(s)
    if not app.state.zones:
        # Without zones every request would fail with UNKNOWN_ZONE; refuse to start instead.
        raise RuntimeError("zones table is empty: run `alembic upgrade head` first")
    app.state.redis = Redis.from_url(settings.REDIS_URL, decode_responses=True)
    await bus.connect()
    await consumers.start(bus, app.state.redis)
    yield
    await bus.close()
    await app.state.redis.aclose()
    await db.dispose()


async def _db_check() -> None:
    async with db.ro() as s:
        await s.execute(text("SELECT 1"))


async def _redis_check() -> None:
    await app.state.redis.ping()


async def _rabbitmq_check() -> None:
    if bus.connection is None or bus.connection.is_closed:
        raise RuntimeError("not connected")


app = FastAPI(title="Tesla Pool Location & Matching", lifespan=lifespan)
install_error_handlers(app)
app.include_router(health_router({"db": _db_check, "redis": _redis_check, "rabbitmq": _rabbitmq_check}))
app.include_router(public.router)
app.include_router(driver.router)
app.include_router(internal.router)
