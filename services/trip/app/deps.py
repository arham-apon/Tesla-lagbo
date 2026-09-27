from tesla_common.auth import InternalAuth
from tesla_common.db import Database
from tesla_common.events import Bus
from tesla_common.http import ServiceClient

from .clients import FareClient, MatchingClient
from .config import settings

db = Database(settings.DB_PATH)
auth = InternalAuth(settings.INTERNAL_TOKEN)
bus = Bus(settings.RABBITMQ_URL)
# Raw HTTP clients are closed by the lifespan (5.8); the routers use the typed wrappers.
fare_http = ServiceClient(settings.FARE_URL, settings.INTERNAL_TOKEN, "fare")
matching_http = ServiceClient(settings.MATCHING_URL, settings.INTERNAL_TOKEN, "matching")
fare_client = FareClient(fare_http)
matching_client = MatchingClient(matching_http)

__all__ = ["auth", "bus", "db", "fare_client", "fare_http", "matching_client", "matching_http", "settings"]
