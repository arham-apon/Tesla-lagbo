import asyncio
from contextlib import asynccontextmanager

from fastapi import FastAPI
from sqlalchemy import select, text
from sqlalchemy.exc import OperationalError

from tesla_common.errors import install_error_handlers
from tesla_common.events import run_outbox_relay
from tesla_common.health import health_router
from tesla_common.logging import configure_logging

from . import settlement
from .deps import bus, db, matching_http, redis, settings
from .models import Outbox, Tariff
from .routers import driver, fares, internal, wallet


async def _require_pricing() -> None:
    # Like Matching and Trip: without tables or a price list every estimate would fail, so refuse to start.
    try:
        async with db.ro() as s:
            tariff = await s.scalar(select(Tariff.id).where(Tariff.active.is_(True)))
    except OperationalError as exc:
        raise RuntimeError("fare.db has no tables: run `alembic upgrade head` first") from exc
    if tariff is None:
        raise RuntimeError("no active tariff: run `alembic upgrade head` (0002 seeds tariff v1)")


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Plan 6.7 step 7: bus connect -> consumer -> outbox relay (Redis and the Matching client are made in deps).
    configure_logging("fare", settings.LOG_LEVEL)
    await _require_pricing()
    await bus.connect()
    await settlement.start(bus, db.rw)
    stop = asyncio.Event()
    relay = asyncio.create_task(run_outbox_relay(db.ro, db.rw, Outbox, bus, stop))
    yield
    stop.set()
    relay.cancel()
    await asyncio.gather(relay, return_exceptions=True)
    await matching_http.aclose()
    await redis.aclose()
    await bus.close()
    await db.dispose()


async def _db_check() -> None:
    async with db.ro() as s:
        await s.execute(text("SELECT 1"))


async def _redis_check() -> None:
    await redis.ping()


async def _rabbitmq_check() -> None:
    if bus.connection is None or bus.connection.is_closed:
        raise RuntimeError("not connected")


app = FastAPI(title="Fare & Billing Service", lifespan=lifespan)
install_error_handlers(app)
app.include_router(fares.router)
app.include_router(wallet.router)
app.include_router(driver.router)
app.include_router(internal.router)
app.include_router(health_router({"db": _db_check, "redis": _redis_check, "rabbitmq": _rabbitmq_check}))
