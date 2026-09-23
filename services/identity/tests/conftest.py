import os
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

SERVICE_DIR = Path(__file__).resolve().parents[1]

# app.config builds Settings() at import time, so the environment must be set first.
os.environ.update(REDIS_URL="redis://unused", RABBITMQ_URL="amqp://unused", INTERNAL_TOKEN="test-internal-token",
                  JWT_PRIVATE_KEY_PATH="unused", DB_PATH="unused.db")

from alembic import command  # noqa: E402
from alembic.config import Config  # noqa: E402

from tesla_common.db import Database  # noqa: E402


def _keypair() -> tuple[str, str]:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                serialization.NoEncryption()).decode()
    public = key.public_key().public_bytes(serialization.Encoding.PEM,
                                           serialization.PublicFormat.SubjectPublicKeyInfo).decode()
    return private, public


PRIVATE_KEY, PUBLIC_KEY = _keypair()


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
