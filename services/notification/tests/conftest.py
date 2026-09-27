import os
from pathlib import Path

import pytest

SERVICE_DIR = Path(__file__).resolve().parents[1]
INTERNAL_TOKEN = "test-internal-token"

# app.config builds Settings() at import time, so the environment must be set first.
os.environ.update(REDIS_URL="redis://unused", RABBITMQ_URL="amqp://unused", INTERNAL_TOKEN=INTERNAL_TOKEN,
                  DB_PATH="unused.db", JWT_PUBLIC_KEY_PATH="unused.pem")

from alembic import command  # noqa: E402
from alembic.config import Config  # noqa: E402

from tesla_common.db import Database  # noqa: E402

# Identity's fixed seed ids (Part 3).
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
    """A fresh notification.db, migrated to head with the real Alembic migrations."""
    path = tmp_path / "notification.db"
    command.upgrade(alembic_config(path), "head")
    return path


@pytest.fixture
async def db(db_path):
    database = Database(str(db_path))
    yield database
    await database.dispose()


async def add(db: Database, *rows) -> None:
    async with db.rw.begin() as s:
        for r in rows:
            s.add(r)
            await s.flush()


# ---- 7.5: login tokens, the WebSocket and the inbox API ----------------------------------------------------------

import time  # noqa: E402
import uuid  # noqa: E402

import jwt  # noqa: E402
from cryptography.hazmat.primitives import serialization  # noqa: E402
from cryptography.hazmat.primitives.asymmetric import rsa  # noqa: E402

GATEWAY = {"X-Internal-Token": INTERNAL_TOKEN}


def as_user(user_id: str, role: str = "PASSENGER") -> dict:
    """The headers the gateway adds after checking the JWT (Part 2)."""
    return GATEWAY | {"X-User-Id": user_id, "X-User-Role": role, "X-User-Name": user_id}


@pytest.fixture(scope="session")
def keys() -> tuple[str, str]:
    """An RS256 key pair standing in for Identity's (Part 3). Returns (private_pem, public_pem)."""
    k = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private = k.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                              serialization.NoEncryption()).decode()
    public = k.public_key().public_bytes(serialization.Encoding.PEM,
                                         serialization.PublicFormat.SubjectPublicKeyInfo).decode()
    return private, public


@pytest.fixture
def make_token(keys):
    """A login token as Identity issues it (Part 3): RS256, issuer tesla-identity, sub/role/jti/iat/exp."""
    def make(user_id: str = NUSRAT, role: str = "PASSENGER", ttl: float = 3600, key: str | None = None,
             **overrides) -> str:
        now = time.time()
        claims = {"sub": user_id, "role": role, "jti": str(uuid.uuid4()), "iat": int(now), "exp": now + ttl,
                  "iss": "tesla-identity"} | overrides
        return jwt.encode(claims, key or keys[0], algorithm="RS256")
    return make


@pytest.fixture
async def api(db, monkeypatch):
    """The inbox router on a test app with the test database, called as the gateway would."""
    import httpx
    from fastapi import FastAPI

    from tesla_common.errors import install_error_handlers

    from app.routers import inbox

    monkeypatch.setattr(inbox, "db", db)
    app = FastAPI()
    install_error_handlers(app)
    app.include_router(inbox.router)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://notification") as c:
        yield c
