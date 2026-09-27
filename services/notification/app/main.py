import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from sqlalchemy import text
from sqlalchemy.exc import OperationalError

from tesla_common.errors import install_error_handlers
from tesla_common.health import health_router
from tesla_common.logging import configure_logging, install_request_context

from . import consumers
from .deps import bus, db, manager, public_key, redis, settings
from .live_location import relay_locations
from .routers import inbox, ws

REDACT = ws.RedactTokenFilter()


async def _preflight() -> None:
    """Refuse to start rather than fail on the first phone (like Matching, Trip and Fare)."""
    try:
        async with db.ro() as s:
            await s.execute(text("SELECT 1 FROM notifications LIMIT 1"))
    except OperationalError as exc:
        raise RuntimeError("notification.db has no tables: run `alembic upgrade head` first") from exc
    try:
        public_key()  # read once, now: a missing key file would otherwise break every socket's login check
    except OSError as exc:
        raise RuntimeError(f"can't read JWT_PUBLIC_KEY_PATH={settings.JWT_PUBLIC_KEY_PATH}") from exc


@asynccontextmanager
async def lifespan(app: FastAPI):
    configure_logging("notification", settings.LOG_LEVEL)
    loggers = [logging.getLogger(n) for n in ("uvicorn.error", "uvicorn.access")]
    for lg in loggers:  # keep login tokens in /ws?token=... out of the log (7.5)
        lg.addFilter(REDACT)
    try:
        await _preflight()
        # Plan step 7.7.6: bus -> consumers -> location relay.
        await bus.connect()
        await consumers.start(bus, db.rw, redis, manager)
        stop = asyncio.Event()
        relay = asyncio.create_task(relay_locations(redis, manager, stop))
        yield
        stop.set()
        relay.cancel()
        await asyncio.gather(relay, return_exceptions=True)
        await bus.close()
        await redis.aclose()
        await db.dispose()
    finally:
        for lg in loggers:  # also when startup is refused
            lg.removeFilter(REDACT)


async def _db_check() -> None:
    async with db.ro() as s:
        await s.execute(text("SELECT 1"))


async def _redis_check() -> None:
    await redis.ping()


async def _rabbitmq_check() -> None:
    if bus.connection is None or bus.connection.is_closed:
        raise RuntimeError("not connected")


app = FastAPI(title="Notification Service", lifespan=lifespan)
install_error_handlers(app)
install_request_context(app)  # 8.6: request id in every log line
app.include_router(ws.router)
app.include_router(inbox.router)
app.include_router(health_router({"db": _db_check, "redis": _redis_check, "rabbitmq": _rabbitmq_check}))
