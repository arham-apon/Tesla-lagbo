from tesla_common.auth import InternalAuth
from tesla_common.db import Database
from tesla_common.http import ServiceClient

from .config import settings

db = Database(settings.DB_PATH)
auth = InternalAuth(settings.INTERNAL_TOKEN)
trip_client = ServiceClient(settings.TRIP_URL, settings.INTERNAL_TOKEN, "trip")
