"""6.5: every endpoint in the plan's table, through HTTP, as the gateway (or Trip) would call it."""
import asyncio
from datetime import timedelta

import httpx
import pytest
import respx

from tesla_common.timeutil import utcnow

from app.models import Fare, Quote, Tariff, Wallet, WalletTransaction
from conftest import GATEWAY, JASHIM, MATCHING, NUSRAT, RAFIQ, add, as_user

DISTANCES = {("BANANI", "MOHAKHALI"): 3500, ("BANANI", "GULSHAN_1"): 2000}  # Matching's real overrides (4.3)
TO_MOHAKHALI = {"pickup_zone": "BANANI", "dropoff_zone": "MOHAKHALI", "seats": 1}


@pytest.fixture
def matching():
    """Fake Matching: answers the two story distances, 422 for anything else."""
    def answer(request):
        pair = (request.url.params["from"], request.url.params["to"])
        if pair in DISTANCES:
            return httpx.Response(200, json={"distance_m": DISTANCES[pair]})
        return httpx.Response(422, json={"error": {"code": "UNKNOWN_ZONE", "message": f"Unknown zone {pair[1]}"}})

    with respx.mock(base_url=MATCHING, assert_all_called=False) as mock:
        yield mock.get("/internal/zones/distance").mock(side_effect=answer)


def error(resp) -> tuple[int, str]:
    return resp.status_code, resp.json()["error"]["code"]


# ---- POST /fares/estimate ---------------------------------------------------------------------------------------

async def test_nusrat_sees_both_prices(api, matching):
    resp = await api.post("/fares/estimate", json=TO_MOHAKHALI, headers=as_user(NUSRAT))
    assert resp.status_code == 201
    q = resp.json()
    assert (q["passenger_id"], q["distance_m"], q["solo_total_poysha"], q["pooled_total_poysha"]) == \
           (NUSRAT, 3500, 8250, 7200)
    assert q["solo"] == {"base_poysha": 3000, "distance_charge_poysha": 5250, "pool_discount_poysha": 0,
                         "total_poysha": 8250}
    assert q["pooled"] == {"base_poysha": 3000, "distance_charge_poysha": 5250, "pool_discount_poysha": 1050,
                           "total_poysha": 7200}


async def test_quote_is_saved_with_its_tariff_and_expires_in_10_minutes(api, db, matching):
    q = (await api.post("/fares/estimate", json=TO_MOHAKHALI, headers=as_user(NUSRAT))).json()
    async with db.ro() as s:
        row = await s.get(Quote, q["quote_id"])
    assert (row.tariff_id, row.solo_total_poysha, row.pooled_total_poysha, row.voided) == (1, 8250, 7200, False)
    assert timedelta(minutes=9, seconds=55) < row.expires_at - utcnow() <= timedelta(minutes=10)


async def test_rafiq_and_two_seats(api, matching):
    rafiq = (await api.post("/fares/estimate", json={"pickup_zone": "BANANI", "dropoff_zone": "GULSHAN_1",
                                                     "seats": 1}, headers=as_user(RAFIQ))).json()
    assert rafiq["pooled_total_poysha"] == 5400
    two = (await api.post("/fares/estimate", json=TO_MOHAKHALI | {"seats": 2}, headers=as_user(NUSRAT))).json()
    assert (two["solo_total_poysha"], two["pooled_total_poysha"]) == (16500, 14400)


async def test_distance_is_asked_once_then_cached(api, matching):
    for _ in range(3):
        await api.post("/fares/estimate", json=TO_MOHAKHALI, headers=as_user(NUSRAT))
    assert matching.call_count == 1


async def test_prices_follow_the_active_tariff(api, db, matching):
    async with db.rw.begin() as s:
        (await s.get(Tariff, 1)).active = False
        s.add(Tariff(id=2, base_poysha=4000, per_km_poysha=2000, pool_discount_pct=25, active=True))
    q = (await api.post("/fares/estimate", json=TO_MOHAKHALI, headers=as_user(NUSRAT))).json()
    assert (q["solo_total_poysha"], q["pooled_total_poysha"]) == (11000, 9250)  # 4000 + 7000; 7000 - 1750


async def test_no_active_tariff_is_503(api, db, matching):
    async with db.rw.begin() as s:
        (await s.get(Tariff, 1)).active = False
    assert error(await api.post("/fares/estimate", json=TO_MOHAKHALI, headers=as_user(NUSRAT))) == \
           (503, "NO_ACTIVE_TARIFF")


@pytest.mark.parametrize("body", [TO_MOHAKHALI | {"dropoff_zone": "BANANI"},  # the 30-taka same-zone quote
                                  TO_MOHAKHALI | {"seats": 0}, TO_MOHAKHALI | {"seats": 7},
                                  TO_MOHAKHALI | {"pickup_zone": "banani"}, {}])
async def test_bad_estimate_422(api, matching, body):
    assert error(await api.post("/fares/estimate", json=body, headers=as_user(NUSRAT))) == (422, "VALIDATION_ERROR")
    assert matching.call_count == 0  # refused at the door, Matching never asked


async def test_unknown_zone(api, matching):
    resp = await api.post("/fares/estimate", json=TO_MOHAKHALI | {"dropoff_zone": "MOTIJHEEL"},
                          headers=as_user(NUSRAT))
    assert error(resp) == (422, "UNKNOWN_ZONE") and resp.json()["error"]["message"] == "Unknown zone MOTIJHEEL"


async def test_only_passengers_estimate(api, matching):
    assert error(await api.post("/fares/estimate", json=TO_MOHAKHALI, headers=as_user(JASHIM, "DRIVER"))) == \
           (403, "FORBIDDEN")
    assert (await api.post("/fares/estimate", json=TO_MOHAKHALI, headers=GATEWAY)).status_code == 401


async def test_request_id_reaches_matching(api, matching):
    await api.post("/fares/estimate", json=TO_MOHAKHALI, headers=as_user(NUSRAT) | {"X-Request-Id": "req-7"})
    assert matching.calls.last.request.headers["x-request-id"] == "req-7"


# ---- /internal/quotes (Trip) ------------------------------------------------------------------------------------

async def test_trip_gets_a_quote_for_nusrat(api, matching):
    resp = await api.post("/internal/quotes", json=TO_MOHAKHALI | {"passenger_id": NUSRAT}, headers=GATEWAY)
    assert resp.status_code == 201
    q = resp.json()
    assert (q["passenger_id"], q["solo_total_poysha"], q["pooled_total_poysha"]) == (NUSRAT, 8250, 7200)
    again = await api.get(f"/internal/quotes/{q['quote_id']}", headers=GATEWAY)
    assert again.status_code == 200 and again.json() == q


async def test_trip_reads_exactly_the_fields_it_needs(api, matching):
    """Trip's FareClient (5.5) parses these into its own Quote model."""
    q = (await api.post("/internal/quotes", json=TO_MOHAKHALI | {"passenger_id": NUSRAT}, headers=GATEWAY)).json()
    assert {"quote_id", "passenger_id", "pickup_zone", "dropoff_zone", "seats", "distance_m", "solo_total_poysha",
            "pooled_total_poysha", "expires_at"} <= set(q)


async def test_quote_not_found(api):
    assert error(await api.get("/internal/quotes/nope", headers=GATEWAY)) == (404, "QUOTE_NOT_FOUND")


async def test_voided_quote_cannot_book_again(api, db, matching):
    """Finding 2 (6.2): when Trip cancels a ride, settlement voids its quote (6.6). It must not book another."""
    q = (await api.post("/internal/quotes", json=TO_MOHAKHALI | {"passenger_id": NUSRAT}, headers=GATEWAY)).json()
    async with db.rw.begin() as s:
        (await s.get(Quote, q["quote_id"])).voided = True
    assert error(await api.get(f"/internal/quotes/{q['quote_id']}", headers=GATEWAY)) == (404, "QUOTE_NOT_FOUND")


async def test_quote_keeps_its_tariff_after_a_price_change(api, db, matching):
    """"Tariff locked at quote time" (6.2): a new price list doesn't change a quote already given."""
    q = (await api.post("/internal/quotes", json=TO_MOHAKHALI | {"passenger_id": NUSRAT}, headers=GATEWAY)).json()
    async with db.rw.begin() as s:
        (await s.get(Tariff, 1)).active = False
        s.add(Tariff(id=2, base_poysha=9999, per_km_poysha=9999, pool_discount_pct=0, active=True))
    assert (await api.get(f"/internal/quotes/{q['quote_id']}", headers=GATEWAY)).json() == q


async def test_internal_needs_the_token(api, matching):
    assert error(await api.post("/internal/quotes", json=TO_MOHAKHALI | {"passenger_id": NUSRAT})) == \
           (401, "UNAUTHORIZED_INTERNAL")
    assert error(await api.get("/internal/quotes/x")) == (401, "UNAUTHORIZED_INTERNAL")


async def test_internal_quote_needs_a_passenger(api, matching):
    assert error(await api.post("/internal/quotes", json=TO_MOHAKHALI, headers=GATEWAY)) == (422, "VALIDATION_ERROR")


# ---- GET /fares/rides/{ride_id} ---------------------------------------------------------------------------------

async def settled_fare(db, **kw):
    q = Quote(id="q-1", passenger_id=NUSRAT, pickup_zone="BANANI", dropoff_zone="MOHAKHALI", seats=1,
              distance_m=3500, tariff_id=1, solo_total_poysha=8250, pooled_total_poysha=7200,
              expires_at=utcnow())
    f = Fare(**{"ride_id": "ride-1", "passenger_id": NUSRAT, "driver_id": JASHIM, "quote_id": "q-1", "seats": 1,
                "pooled": True, "base_poysha": 3000, "distance_charge_poysha": 5250, "pool_discount_poysha": 1050,
                "total_poysha": 7200, "payment_method": "WALLET", "payment_status": "PAID"} | kw)
    await add(db, q, f)
    return f


async def test_nusrat_and_jashim_see_the_fare(api, db):
    await settled_fare(db)
    for who in (as_user(NUSRAT), as_user(JASHIM, "DRIVER")):
        resp = await api.get("/fares/rides/ride-1", headers=who)
        assert resp.status_code == 200
        body = resp.json()
        assert (body["total_poysha"], body["pool_discount_poysha"], body["payment_status"]) == (7200, 1050, "PAID")


async def test_nobody_else_sees_it(api, db):
    await settled_fare(db)
    assert error(await api.get("/fares/rides/ride-1", headers=as_user(RAFIQ))) == (404, "FARE_NOT_FOUND")
    assert error(await api.get("/fares/rides/ride-1", headers=as_user("driver-karim", "DRIVER"))) == \
           (404, "FARE_NOT_FOUND")
    # a passenger id used as a "driver" (or the other way round) doesn't work either
    assert error(await api.get("/fares/rides/ride-1", headers=as_user(NUSRAT, "DRIVER"))) == (404, "FARE_NOT_FOUND")


async def test_not_settled_yet(api):
    assert error(await api.get("/fares/rides/ride-9", headers=as_user(NUSRAT))) == (404, "FARE_NOT_FOUND")


# ---- /wallet ----------------------------------------------------------------------------------------------------

async def test_no_wallet_yet_is_zero(api):
    resp = await api.get("/wallet", headers=as_user(NUSRAT))
    assert resp.status_code == 200 and resp.json() == {"balance_poysha": 0, "transactions": []}


async def test_top_up_creates_then_adds(api, db):
    assert (await api.post("/wallet/topup", json={"amount_poysha": 50000}, headers=as_user(NUSRAT))).json() == \
           {"balance_poysha": 50000}
    assert (await api.post("/wallet/topup", json={"amount_poysha": 2500}, headers=as_user(NUSRAT))).json() == \
           {"balance_poysha": 52500}
    w = (await api.get("/wallet", headers=as_user(NUSRAT))).json()
    assert w["balance_poysha"] == 52500
    assert sorted(t["amount_poysha"] for t in w["transactions"]) == [2500, 50000]
    assert all(t["kind"] == "TOPUP" and t["ride_id"] is None for t in w["transactions"])


@pytest.mark.parametrize("amount", [0, -100, 500_001])
async def test_top_up_limits(api, amount):
    assert error(await api.post("/wallet/topup", json={"amount_poysha": amount}, headers=as_user(NUSRAT))) == \
           (422, "VALIDATION_ERROR")


async def test_top_up_upper_limit_is_allowed(api):
    assert (await api.post("/wallet/topup", json={"amount_poysha": 500_000}, headers=as_user(NUSRAT))).json() == \
           {"balance_poysha": 500_000}


async def test_drivers_cannot_top_up_but_can_see_their_wallet(api, db):
    assert error(await api.post("/wallet/topup", json={"amount_poysha": 100}, headers=as_user(JASHIM, "DRIVER"))) \
           == (403, "FORBIDDEN")
    await add(db, Wallet(user_id=JASHIM, balance_poysha=7200),
              WalletTransaction(user_id=JASHIM, ride_id="ride-1", kind="DRIVER_CREDIT", amount_poysha=7200))
    w = (await api.get("/wallet", headers=as_user(JASHIM, "DRIVER"))).json()
    assert (w["balance_poysha"], w["transactions"][0]["kind"], w["transactions"][0]["ride_id"]) == \
           (7200, "DRIVER_CREDIT", "ride-1")


async def test_wallets_are_private(api):
    await api.post("/wallet/topup", json={"amount_poysha": 50000}, headers=as_user(NUSRAT))
    assert (await api.get("/wallet", headers=as_user(RAFIQ))).json() == {"balance_poysha": 0, "transactions": []}


async def test_transactions_newest_first_and_limited(api):
    for amount in (100, 200, 300):
        await api.post("/wallet/topup", json={"amount_poysha": amount}, headers=as_user(NUSRAT))
    w = (await api.get("/wallet?limit=2", headers=as_user(NUSRAT))).json()
    assert [t["amount_poysha"] for t in w["transactions"]] == [300, 200] and w["balance_poysha"] == 600


async def test_concurrent_top_ups_all_count(api):
    await asyncio.gather(*(api.post("/wallet/topup", json={"amount_poysha": 1000}, headers=as_user(NUSRAT))
                           for _ in range(10)))
    w = (await api.get("/wallet", headers=as_user(NUSRAT))).json()
    assert w["balance_poysha"] == 10_000 and len(w["transactions"]) == 10


# ---- GET /driver/earnings ---------------------------------------------------------------------------------------

async def test_earnings(api, db):
    q = Quote(id="q-1", passenger_id=NUSRAT, pickup_zone="BANANI", dropoff_zone="MOHAKHALI", seats=1,
              distance_m=3500, tariff_id=1, solo_total_poysha=8250, pooled_total_poysha=7200, expires_at=utcnow())

    def f(ride, total, method, status, driver=JASHIM):
        return Fare(ride_id=ride, passenger_id=NUSRAT, driver_id=driver, quote_id="q-1", seats=1, pooled=False,
                    base_poysha=3000, distance_charge_poysha=total - 3000, pool_discount_poysha=0,
                    total_poysha=total, payment_method=method, payment_status=status)

    await add(db, q, f("r1", 7200, "WALLET", "PAID"), f("r2", 5400, "CASH", "PAID"),
              f("r3", 8250, "WALLET", "FAILED"),                 # wallet short: Jashim collected cash
              f("r4", 6000, "WALLET", "REFUNDED"),               # refunded: not earned
              f("r5", 9999, "CASH", "PAID", driver="driver-karim"))  # someone else's
    resp = await api.get("/driver/earnings", headers=as_user(JASHIM, "DRIVER"))
    assert resp.status_code == 200
    assert resp.json() == {"rides": 3, "total_poysha": 20850, "cash_poysha": 13650, "wallet_poysha": 7200}


async def test_no_rides_yet(api):
    assert (await api.get("/driver/earnings", headers=as_user(JASHIM, "DRIVER"))).json() == \
           {"rides": 0, "total_poysha": 0, "cash_poysha": 0, "wallet_poysha": 0}


async def test_passengers_have_no_earnings(api):
    assert error(await api.get("/driver/earnings", headers=as_user(NUSRAT))) == (403, "FORBIDDEN")
