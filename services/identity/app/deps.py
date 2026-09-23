from functools import lru_cache
from pathlib import Path

from redis.asyncio import Redis

from tesla_common.auth import InternalAuth
from tesla_common.db import Database
from tesla_common.http import ServiceClient

from .config import settings

db = Database(settings.DB_PATH)
auth = InternalAuth(settings.INTERNAL_TOKEN)
trip_client = ServiceClient(settings.TRIP_URL, settings.INTERNAL_TOKEN, "trip")
redis = Redis.from_url(settings.REDIS_URL, decode_responses=True)


@lru_cache
def private_key() -> str:
    return Path(settings.JWT_PRIVATE_KEY_PATH).read_text()
