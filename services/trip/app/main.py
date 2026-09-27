import asyncio
from contextlib import asynccontextmanager

from fastapi import FastAPI
from sqlalchemy import text
from sqlalchemy.exc import OperationalError

from tesla_common.errors import install_error_handlers
from tesla_common.events import run_outbox_relay
from tesla_common.health import health_router
from tesla_common.logging import configure_logging, install_request_context

from . import consumers
from .deps import bus, db, fare_http, matching_http, settings
from .models import Outbox
from .routers import driver, internal, passenger
from .workers import expire_stale_requests


async def _require_schema() -> None:
    # Like Matching: without the tables every request would be a 500, so refuse to start with a clear message.
    try:
        async with db.ro() as s:
            await s.execute(text("SELECT 1 FROM pools LIMIT 1"))
    except OperationalError as exc:
        raise RuntimeError("trip.db has no tables: run `alembic upgrade head` first") from exc


@asynccontextmanager
async def lifespan(app: FastAPI):
    configure_logging("trip-service", settings.LOG_LEVEL)
    await _require_schema()
    await bus.connect()
    await consumers.start(bus, db.rw)
    stop = asyncio.Event()
    tasks = [
        asyncio.create_task(run_outbox_relay(db.ro, db.rw, Outbox, bus, stop)),
        asyncio.create_task(expire_stale_requests(db.ro, db.rw, settings.RIDE_REQUEST_TTL_SECONDS, stop)),
    ]
    yield
    stop.set()
    for t in tasks:
        t.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    await fare_http.aclose()
    await matching_http.aclose()
    await bus.close()
    await db.dispose()


async def _db_check():
    async with db.ro() as s:
        await s.execute(text("SELECT 1"))


async def _bus_check():
    if bus.connection is None or bus.connection.is_closed:
        raise RuntimeError("rabbitmq disconnected")


app = FastAPI(title="Trip & Pooling Service", lifespan=lifespan)
install_error_handlers(app)
install_request_context(app)  # 8.6: request id in every log line
app.include_router(passenger.router)
app.include_router(driver.router)
app.include_router(internal.router)
app.include_router(health_router({"db": _db_check, "rabbitmq": _bus_check}))
