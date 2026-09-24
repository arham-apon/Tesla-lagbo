"""6.5: Trip's REAL FareClient (services/trip/app/clients.py, 5.5) against Fare's REAL routers. Each service was
tested against the plan on its own; this proves the two actually fit. Trip's clients.py imports nothing from
Trip's app, so it can be loaded here under another name."""
import importlib.util

import httpx
import pytest
import respx

from tesla_common.errors import DomainError
from tesla_common.http import ServiceClient

from app.models import Quote
from conftest import INTERNAL_TOKEN, MATCHING, NUSRAT, RAFIQ, SERVICE_DIR

_spec = importlib.util.spec_from_file_location("trip_clients", SERVICE_DIR.parent / "trip" / "app" / "clients.py")
trip_clients = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(trip_clients)


@pytest.fixture
async def trip_fare_client(api):
    """Trip's FareClient, its HTTP calls going straight into Fare's test app (with the internal token)."""
    http = ServiceClient("http://fare", INTERNAL_TOKEN, "fare")
    await http.aclose()  # swap its network client for one that calls the test app in-process
    http._client = httpx.AsyncClient(transport=api._transport, base_url="http://fare",
                                     headers={"X-Internal-Token": INTERNAL_TOKEN})
    yield trip_clients.FareClient(http)
    await http.aclose()


@pytest.fixture(autouse=True)
def matching():
    with respx.mock(base_url=MATCHING, assert_all_called=False) as mock:
        mock.get("/internal/zones/distance", params={"from": "BANANI", "to": "MOHAKHALI"}).mock(
            return_value=httpx.Response(200, json={"distance_m": 3500}))
        mock.get("/internal/zones/distance").mock(return_value=httpx.Response(
            422, json={"error": {"code": "UNKNOWN_ZONE", "message": "Unknown zone MOTIJHEEL"}}))
        yield


async def test_new_quote(trip_fare_client):
    q = await trip_fare_client.quote_for(NUSRAT, "BANANI", "MOHAKHALI", 1, None, "req-1")
    assert (q.passenger_id, q.distance_m, q.solo_total_poysha, q.pooled_total_poysha) == (NUSRAT, 3500, 8250, 7200)


async def test_the_apps_quote_is_accepted(trip_fare_client):
    first = await trip_fare_client.quote_for(NUSRAT, "BANANI", "MOHAKHALI", 1, None, None)
    again = await trip_fare_client.quote_for(NUSRAT, "BANANI", "MOHAKHALI", 1, first.quote_id, None)
    assert again == first  # Fare's naive expires_at is understood by Trip's expiry check


async def test_someone_elses_quote_is_refused(trip_fare_client):
    q = await trip_fare_client.quote_for(NUSRAT, "BANANI", "MOHAKHALI", 1, None, None)
    with pytest.raises(DomainError) as e:
        await trip_fare_client.quote_for(RAFIQ, "BANANI", "MOHAKHALI", 1, q.quote_id, None)
    assert e.value.code == "QUOTE_MISMATCH"


async def test_expired_quote_is_refused(trip_fare_client, db):
    q = await trip_fare_client.quote_for(NUSRAT, "BANANI", "MOHAKHALI", 1, None, None)
    async with db.rw.begin() as s:
        row = await s.get(Quote, q.quote_id)
        row.expires_at = row.created_at
    with pytest.raises(DomainError) as e:
        await trip_fare_client.quote_for(NUSRAT, "BANANI", "MOHAKHALI", 1, q.quote_id, None)
    assert e.value.code == "QUOTE_EXPIRED"


async def test_voided_or_missing_quote_is_not_found(trip_fare_client, db):
    q = await trip_fare_client.quote_for(NUSRAT, "BANANI", "MOHAKHALI", 1, None, None)
    async with db.rw.begin() as s:
        (await s.get(Quote, q.quote_id)).voided = True
    for quote_id in (q.quote_id, "no-such-quote"):
        with pytest.raises(DomainError) as e:
            await trip_fare_client.quote_for(NUSRAT, "BANANI", "MOHAKHALI", 1, quote_id, None)
        assert (e.value.code, e.value.status) == ("QUOTE_NOT_FOUND", 422)


async def test_unknown_zone_reaches_trip_with_its_message(trip_fare_client):
    with pytest.raises(DomainError) as e:
        await trip_fare_client.quote_for(NUSRAT, "BANANI", "MOTIJHEEL", 1, None, None)
    assert (e.value.code, e.value.status, e.value.message) == ("UNKNOWN_ZONE", 422, "Unknown zone MOTIJHEEL")

