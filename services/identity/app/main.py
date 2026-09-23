import asyncio
from contextlib import asynccontextmanager, suppress

from fastapi import FastAPI
from sqlalchemy import text

from tesla_common.errors import install_error_handlers
from tesla_common.events import run_outbox_relay
from tesla_common.health import health_router
from tesla_common.logging import configure_logging

from .config import settings
from .deps import bus, db, redis, trip_client
from .models import Outbox
from .routers import auth, drivers, internal


@asynccontextmanager
async def lifespan(app: FastAPI):
    configure_logging("identity", settings.LOG_LEVEL)
    await bus.connect()
    stop = asyncio.Event()
    relay = asyncio.create_task(run_outbox_relay(db.ro, db.rw, Outbox, bus, stop))
    yield
    stop.set()
    # Let the relay finish its current batch; cancel only if it hangs (a cut-off batch is re-sent, never lost).
    with suppress(asyncio.TimeoutError, asyncio.CancelledError):
        await asyncio.wait_for(relay, timeout=5)
    await bus.close()
    await trip_client.aclose()
    await redis.aclose()
    await db.dispose()


async def _db_check() -> None:
    async with db.ro() as s:
        await s.execute(text("SELECT 1"))


async def _rabbitmq_check() -> None:
    if bus.connection is None or bus.connection.is_closed:
        raise RuntimeError("not connected")


async def _redis_check() -> None:
    await redis.ping()


app = FastAPI(title="Tesla Pool Identity", lifespan=lifespan)
install_error_handlers(app)
app.include_router(health_router({"db": _db_check, "rabbitmq": _rabbitmq_check, "redis": _redis_check}))
app.include_router(auth.router)
app.include_router(drivers.router)
app.include_router(internal.router)
