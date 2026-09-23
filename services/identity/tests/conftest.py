import os
import tempfile
from pathlib import Path

import fakeredis
import httpx
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

SERVICE_DIR = Path(__file__).resolve().parents[1]
INTERNAL_TOKEN = "test-internal-token"


def _keypair() -> tuple[str, str]:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                serialization.NoEncryption()).decode()
    public = key.public_key().public_bytes(serialization.Encoding.PEM,
                                           serialization.PublicFormat.SubjectPublicKeyInfo).decode()
    return private, public


PRIVATE_KEY, PUBLIC_KEY = _keypair()
_key_file = Path(tempfile.mkdtemp()) / "jwt_private.pem"
_key_file.write_text(PRIVATE_KEY)

# app.config builds Settings() at import time, so the environment must be set first.
os.environ.update(REDIS_URL="redis://unused", RABBITMQ_URL="amqp://unused", INTERNAL_TOKEN=INTERNAL_TOKEN,
                  JWT_PRIVATE_KEY_PATH=str(_key_file), DB_PATH="unused.db", TRIP_URL="http://trip:8003")

from alembic import command  # noqa: E402
from alembic.config import Config  # noqa: E402
from fastapi import FastAPI  # noqa: E402

from tesla_common.db import Database  # noqa: E402
from tesla_common.errors import install_error_handlers  # noqa: E402
from tesla_common.http import ServiceClient  # noqa: E402

from app.routers import auth as auth_router, drivers as drivers_router, internal as internal_router  # noqa: E402


def alembic_config(db_path: Path) -> Config:
    cfg = Config(str(SERVICE_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(SERVICE_DIR / "migrations"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite+aiosqlite:///{db_path.as_posix()}")
    return cfg


@pytest.fixture
def db_path(tmp_path) -> Path:
    """A fresh identity.db, migrated to head with the real Alembic migrations."""
    path = tmp_path / "identity.db"
    command.upgrade(alembic_config(path), "head")
    return path


@pytest.fixture
async def db(db_path):
    """tesla_common.Database on the migrated file: WAL, foreign keys ON, BEGIN IMMEDIATE writes."""
    database = Database(str(db_path))
    yield database
    await database.dispose()


@pytest.fixture
async def redis():
    r = fakeredis.aioredis.FakeRedis(decode_responses=True)
    yield r
    await r.aclose()


class FakeTrip:
    """Stands in for Trip's GET /internal/drivers/{id}/live-pool (Trip is built in Part 5)."""

    def __init__(self):
        self.calls: list[httpx.Request] = []
        self.pool_id: str | None = None
        self.status = 200
        self.down = False

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(request)
        if self.down:
            raise httpx.ConnectError("connection refused", request=request)
        if self.status != 200:
            return httpx.Response(self.status, json={"error": {"code": "SOMETHING"}})
        return httpx.Response(200, json={"pool_id": self.pool_id})


@pytest.fixture
def trip() -> FakeTrip:
    return FakeTrip()


@pytest.fixture
async def api(db, redis, trip, monkeypatch):
    """The three routers on a test app, wired to the temp DB, fake Redis and fake Trip.
    (main.py with the RabbitMQ bus and outbox relay is plan step 3.6.6.)"""
    client = ServiceClient("http://trip:8003", INTERNAL_TOKEN, "trip")
    client._client = httpx.AsyncClient(base_url="http://trip:8003", transport=httpx.MockTransport(trip),
                                       headers={"X-Internal-Token": INTERNAL_TOKEN})
    for module in (auth_router, drivers_router, internal_router):
        monkeypatch.setattr(module, "db", db)
    monkeypatch.setattr(auth_router, "redis", redis)
    monkeypatch.setattr(drivers_router, "trip_client", client)

    app = FastAPI()
    install_error_handlers(app)
    for module in (auth_router, drivers_router, internal_router):
        app.include_router(module.router)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://identity") as c:
        yield c
    await client.aclose()


def as_user(user_id: str, role: str = "PASSENGER", name: str = "", **extra) -> dict:
    """Headers exactly as the gateway sends them after verifying a token."""
    return {"X-Internal-Token": INTERNAL_TOKEN, "X-User-Id": user_id, "X-User-Role": role, "X-User-Name": name} | extra


GATEWAY = {"X-Internal-Token": INTERNAL_TOKEN}
