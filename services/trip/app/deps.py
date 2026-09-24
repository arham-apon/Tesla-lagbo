from tesla_common.auth import InternalAuth
from tesla_common.db import Database
from tesla_common.events import Bus
from tesla_common.http import ServiceClient

from .config import settings

db = Database(settings.DB_PATH)
auth = InternalAuth(settings.INTERNAL_TOKEN)
bus = Bus(settings.RABBITMQ_URL)
# Raw HTTP clients, closed by the lifespan (5.8). The typed FareClient/MatchingClient wrappers join in 5.5.
fare_http = ServiceClient(settings.FARE_URL, settings.INTERNAL_TOKEN, "fare")
matching_http = ServiceClient(settings.MATCHING_URL, settings.INTERNAL_TOKEN, "matching")

__all__ = ["auth", "bus", "db", "fare_http", "matching_http", "settings"]
