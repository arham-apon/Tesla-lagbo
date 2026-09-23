from tesla_common.auth import InternalAuth
from tesla_common.db import Database
from tesla_common.events import Bus

from .config import settings

db = Database(settings.DB_PATH)
auth = InternalAuth(settings.INTERNAL_TOKEN)
bus = Bus(settings.RABBITMQ_URL)

__all__ = ["auth", "bus", "db", "settings"]
