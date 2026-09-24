from redis.asyncio import Redis

from tesla_common.auth import InternalAuth
from tesla_common.db import Database
from tesla_common.events import Bus
from tesla_common.http import ServiceClient

from .config import settings

db = Database(settings.DB_PATH)
auth = InternalAuth(settings.INTERNAL_TOKEN)
bus = Bus(settings.RABBITMQ_URL)
redis = Redis.from_url(settings.REDIS_URL, decode_responses=True)  # the 24 h distance cache (6.4)
matching_http = ServiceClient(settings.MATCHING_URL, settings.INTERNAL_TOKEN, "matching")

__all__ = ["auth", "bus", "db", "matching_http", "redis", "settings"]
