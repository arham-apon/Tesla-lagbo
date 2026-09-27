from functools import lru_cache
from pathlib import Path

from redis.asyncio import Redis

from tesla_common.auth import InternalAuth
from tesla_common.db import Database
from tesla_common.events import Bus

from .config import settings
from .connections import ConnectionManager

db = Database(settings.DB_PATH)
auth = InternalAuth(settings.INTERNAL_TOKEN)
bus = Bus(settings.RABBITMQ_URL)
redis = Redis.from_url(settings.REDIS_URL, decode_responses=True)  # denylist, pool members, live locations
manager = ConnectionManager()  # this copy's open sockets (7.4)


@lru_cache
def public_key() -> str:
    """Identity's RS256 public key, read once (like Identity's private_key())."""
    return Path(settings.JWT_PUBLIC_KEY_PATH).read_text()


__all__ = ["auth", "bus", "db", "manager", "public_key", "redis", "settings"]
