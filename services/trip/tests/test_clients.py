"""5.5 / 5.9.3: FareClient and MatchingClient against HTTP answers faked with respx."""
from datetime import timedelta

import httpx
import pytest
import respx

from tesla_common.errors import DomainError
from tesla_common.http import ServiceClient
from tesla_common.timeutil import utcnow

from app.clients import FareClient, MatchingClient
from conftest import INTERNAL_TOKEN, NUSRAT

FARE, MATCHING = "http://fare.test", "http://matching.test"


def quote_json(**kw) -> dict:
    return {"quote_id": "q-1", "passenger_id": NUSRAT, "pickup_zone": "BANANI", "dropoff_zone": "MOHAKHALI",
            "seats": 1, "distance_m": 3500, "solo_total_poysha": 11000, "pooled_total_poysha": 8800,
            "expires_at": (utcnow() + timedelta(minutes=5)).isoformat()} | kw


@pytest.fixture
async def fare():
    http = ServiceClient(FARE, INTERNAL_TOKEN, "fare")
    yield FareClient(http)
    await http.aclose()


@pytest.fixture
async def matching():
    http = ServiceClient(MATCHING, INTERNAL_TOKEN, "matching")
    yield MatchingClient(http)
    await http.aclose()


async def code_of(coro) -> tuple[str, int]:
    with pytest.raises(DomainError) as e:
        await coro
    return e.value.code, e.value.status


def ask(fare, quote_id=None, **kw):
    args = {"passenger_id": NUSRAT, "pickup": "BANANI", "dropoff": "MOHAKHALI", "seats": 1} | kw
    return fare.quote_for(args["passenger_id"], args["pickup"], args["dropoff"], args["seats"], quote_id, "req-1")


# ---- Fare: a new quote -----------------------------------------------------------------------------------------

@respx.mock
async def test_new_quote(fare):
    route = respx.post(f"{FARE}/internal/quotes").mock(return_value=httpx.Response(201, json=quote_json()))
    q = await ask(fare)
    assert (q.quote_id, q.distance_m, q.solo_total_poysha, q.pooled_total_poysha) == ("q-1", 3500, 11000, 8800)
    sent = route.calls.last.request
    assert sent.headers["x-internal-token"] == INTERNAL_TOKEN and sent.headers["x-request-id"] == "req-1"
    assert httpx.Response(200, content=sent.content).json() == {
        "passenger_id": NUSRAT, "pickup_zone": "BANANI", "dropoff_zone": "MOHAKHALI", "seats": 1}


@respx.mock
async def test_unknown_zone(fare):
    respx.post(f"{FARE}/internal/quotes").mock(return_value=httpx.Response(
        422, json={"error": {"code": "UNKNOWN_ZONE", "message": "Unknown zone MOTIJHEEL"}}))
    with pytest.raises(DomainError) as e:
        await ask(fare, dropoff="MOTIJHEEL")
    assert (e.value.code, e.value.status, e.value.message) == ("UNKNOWN_ZONE", 422, "Unknown zone MOTIJHEEL")


# ---- Fare: the quote the app already has ----------------------------------------------------------------------

@respx.mock
async def test_existing_quote(fare):
    respx.get(f"{FARE}/internal/quotes/q-1").mock(return_value=httpx.Response(200, json=quote_json()))
    assert (await ask(fare, "q-1")).quote_id == "q-1"


@respx.mock
async def test_existing_quote_not_found(fare):
    respx.get(f"{FARE}/internal/quotes/q-9").mock(return_value=httpx.Response(404, json={}))
    assert await code_of(ask(fare, "q-9")) == ("QUOTE_NOT_FOUND", 422)


@respx.mock
@pytest.mark.parametrize("change", [{"passenger_id": "passenger-rafiq"}, {"pickup_zone": "MIRPUR"},
                                    {"dropoff_zone": "GULSHAN_1"}, {"seats": 2}])
async def test_quote_must_match_the_request(fare, change):
    # Rafiq can't use Nusrat's cheaper quote; a 1-seat quote can't book 2 seats.
    respx.get(f"{FARE}/internal/quotes/q-1").mock(return_value=httpx.Response(200, json=quote_json(**change)))
    assert await code_of(ask(fare, "q-1")) == ("QUOTE_MISMATCH", 422)


@respx.mock
async def test_expired_quote(fare):
    old = (utcnow() - timedelta(seconds=1)).isoformat()
    respx.get(f"{FARE}/internal/quotes/q-1").mock(return_value=httpx.Response(200, json=quote_json(expires_at=old)))
    assert await code_of(ask(fare, "q-1")) == ("QUOTE_EXPIRED", 422)


@respx.mock
@pytest.mark.parametrize("suffix, minutes, expired", [("Z", 5, False), ("Z", -5, True),
                                                      ("+06:00", 5, False), ("+06:00", -5, True)])
async def test_expiry_with_a_timezone(fare, suffix, minutes, expired):
    # Fix: the plan compared with datetime.utcnow(), which raises TypeError (a 500) for "...Z" times.
    moment = utcnow() + timedelta(minutes=minutes) + (timedelta(hours=6) if suffix == "+06:00" else timedelta())
    body = quote_json(expires_at=moment.isoformat() + suffix)
    respx.get(f"{FARE}/internal/quotes/q-1").mock(return_value=httpx.Response(200, json=body))
    if expired:
        assert await code_of(ask(fare, "q-1")) == ("QUOTE_EXPIRED", 422)
    else:
        assert (await ask(fare, "q-1")).quote_id == "q-1"


# ---- Fare: failures ---------------------------------------------------------------------------------------------

@respx.mock
@pytest.mark.parametrize("status", [400, 401, 403, 404])
async def test_unexpected_answer_for_a_new_quote_is_503(fare, status):
    # Fix: only 5xx used to become an error; a 401 (bad internal token) crashed as a 500 while parsing a quote.
    respx.post(f"{FARE}/internal/quotes").mock(return_value=httpx.Response(status, json={"error": {}}))
    assert await code_of(ask(fare)) == ("UPSTREAM_ERROR", 503)


@respx.mock
async def test_unexpected_answer_for_an_existing_quote_is_503(fare):
    respx.get(f"{FARE}/internal/quotes/q-1").mock(return_value=httpx.Response(401, json={"error": {}}))
    assert await code_of(ask(fare, "q-1")) == ("UPSTREAM_ERROR", 503)


@respx.mock
async def test_fare_down(fare):
    respx.post(f"{FARE}/internal/quotes").mock(side_effect=httpx.ConnectError("refused"))
    assert await code_of(ask(fare)) == ("UPSTREAM_UNAVAILABLE", 503)


@respx.mock
async def test_fare_500(fare):
    respx.post(f"{FARE}/internal/quotes").mock(return_value=httpx.Response(500))
    assert await code_of(ask(fare)) == ("UPSTREAM_ERROR", 503)


# ---- Matching ---------------------------------------------------------------------------------------------------

EVALUATION = {"solo_distance_m": 3500, "compatible_pools": [], "candidate_drivers": [
    {"driver_id": "driver-jashim", "distance_m": 800}]}


@respx.mock
async def test_evaluate(matching):
    route = respx.post(f"{MATCHING}/internal/match/evaluate").mock(return_value=httpx.Response(200, json=EVALUATION))
    payload = {"pickup_zone": "BANANI", "dropoff_zone": "MOHAKHALI", "seats": 1, "open_pools": []}
    assert await matching.evaluate(payload, "req-1") == EVALUATION
    assert route.calls.last.request.headers["x-internal-token"] == INTERNAL_TOKEN


@respx.mock
async def test_evaluate_unknown_zone(matching):
    respx.post(f"{MATCHING}/internal/match/evaluate").mock(return_value=httpx.Response(422, json={}))
    assert await code_of(matching.evaluate({}, None)) == ("UNKNOWN_ZONE", 422)


@respx.mock
async def test_evaluate_unexpected_answer_is_503(matching):
    respx.post(f"{MATCHING}/internal/match/evaluate").mock(return_value=httpx.Response(401, json={"error": {}}))
    assert await code_of(matching.evaluate({}, None)) == ("UPSTREAM_ERROR", 503)


@respx.mock
async def test_matching_down(matching):
    respx.post(f"{MATCHING}/internal/match/evaluate").mock(side_effect=httpx.ReadTimeout("slow"))
    assert await code_of(matching.evaluate({}, None)) == ("UPSTREAM_UNAVAILABLE", 503)
