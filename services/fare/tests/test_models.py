"""6.4: the database refuses wrong money, whatever the code does."""
from datetime import datetime

import pytest
from sqlalchemy import update
from sqlalchemy.exc import IntegrityError

from app.models import Fare, Quote, Tariff, Wallet, WalletTransaction
from conftest import JASHIM, NUSRAT, add


def quote(**kw) -> Quote:
    return Quote(**{"id": "q-1", "passenger_id": NUSRAT, "pickup_zone": "BANANI", "dropoff_zone": "MOHAKHALI",
                    "seats": 1, "distance_m": 3500, "tariff_id": 1, "solo_total_poysha": 8250,
                    "pooled_total_poysha": 7200, "expires_at": datetime(2099, 1, 1)} | kw)


def fare(**kw) -> Fare:
    """Nusrat's pooled 72.00-taka fare (plan 6.2)."""
    return Fare(**{"ride_id": "ride-1", "passenger_id": NUSRAT, "driver_id": JASHIM, "quote_id": "q-1", "seats": 1,
                   "pooled": True, "base_poysha": 3000, "distance_charge_poysha": 5250, "pool_discount_poysha": 1050,
                   "total_poysha": 7200, "payment_method": "WALLET", "payment_status": "PAID"} | kw)


async def fails(db, *rows) -> None:
    with pytest.raises(IntegrityError):
        await add(db, *rows)


# ---- tariffs ----------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("bad", [{"base_poysha": -1}, {"per_km_poysha": -1}, {"pool_discount_pct": 101},
                                 {"pool_discount_pct": -1}])
async def test_tariff_rules(db, bad):
    await fails(db, Tariff(**{"id": 2, "base_poysha": 3000, "per_km_poysha": 1500, "pool_discount_pct": 20,
                              "active": False} | bad))


async def test_only_one_active_tariff(db):
    await fails(db, Tariff(id=2, base_poysha=3500, per_km_poysha=1600, pool_discount_pct=20, active=True))


async def test_new_price_list_the_right_way(db):
    """Retire tariff 1, add tariff 2 as the active one, in one transaction. Tariff 1 stays for old quotes."""
    async with db.rw.begin() as s:
        await s.execute(update(Tariff).where(Tariff.id == 1).values(active=False))
        s.add(Tariff(id=2, base_poysha=3500, per_km_poysha=1600, pool_discount_pct=20, active=True))


# ---- quotes -----------------------------------------------------------------------------------------------------

async def test_valid_quote(db):
    q = quote()
    await add(db, q)
    assert (q.voided, q.created_at is not None) == (False, True)


@pytest.mark.parametrize("bad", [
    {"seats": 0}, {"seats": 7},
    {"dropoff_zone": "BANANI"},                       # the 30-taka same-zone quote (found in 6.2)
    {"distance_m": 0},
    {"pooled_total_poysha": 9000},                    # pooled dearer than solo
    {"pooled_total_poysha": -1},
    {"tariff_id": 99},                                # no such price list
])
async def test_quote_rules(db, bad):
    await fails(db, quote(**bad))


# ---- fares (the ledger) -----------------------------------------------------------------------------------------

async def test_nusrats_fare(db):
    await add(db, quote(), fare())


@pytest.mark.parametrize("bad", [
    {"total_poysha": 7100},                                          # doesn't add up
    {"pooled": False},                                               # a discount on a solo ride
    {"pool_discount_poysha": -1050, "total_poysha": 9300},           # a negative "discount" that adds up
    {"seats": 0}, {"seats": 7},
    {"payment_method": "BKASH"}, {"payment_status": "PENDING"},
    {"quote_id": "no-such-quote"},
])
async def test_fare_rules(db, bad):
    await add(db, quote())
    await fails(db, fare(**bad))


async def test_solo_fare_without_discount_is_fine(db):
    await add(db, quote(), fare(pooled=False, pool_discount_poysha=0, total_poysha=8250))


async def test_one_fare_per_ride(db):
    await add(db, quote(), fare())
    await fails(db, fare(payment_status="FAILED"))


async def test_multi_seat_fare_adds_up(db):
    # ck_fare_arithmetic stores totals for all seats (plan note in 6.4).
    await add(db, quote(seats=2, solo_total_poysha=16500, pooled_total_poysha=14400),
              fare(seats=2, base_poysha=6000, distance_charge_poysha=10500, pool_discount_poysha=2100,
                   total_poysha=14400))


# ---- wallets ----------------------------------------------------------------------------------------------------

async def test_wallet_cannot_go_negative(db):
    await add(db, Wallet(user_id=NUSRAT, balance_poysha=5000))
    with pytest.raises(IntegrityError):
        async with db.rw.begin() as s:
            await s.execute(update(Wallet).where(Wallet.user_id == NUSRAT).values(
                balance_poysha=Wallet.balance_poysha - 7200))


async def test_new_wallet_starts_at_zero(db):
    w = Wallet(user_id=JASHIM)
    await add(db, w)
    assert w.balance_poysha == 0


def txn(**kw) -> WalletTransaction:
    return WalletTransaction(**{"user_id": NUSRAT, "ride_id": "ride-1", "kind": "RIDE_DEBIT",
                                "amount_poysha": -7200} | kw)


@pytest.mark.parametrize("row", [
    {"kind": "RIDE_DEBIT", "amount_poysha": -7200},
    {"kind": "TOPUP", "amount_poysha": 50000, "ride_id": None},
    {"kind": "DRIVER_CREDIT", "amount_poysha": 7200, "user_id": JASHIM},
])
async def test_money_moving_the_right_way(db, row):
    await add(db, Wallet(user_id=NUSRAT), Wallet(user_id=JASHIM), txn(**row))


@pytest.mark.parametrize("bad", [
    {"amount_poysha": 7200},                                         # a "debit" that gives money
    {"kind": "TOPUP", "amount_poysha": -500, "ride_id": None},       # a "top-up" that takes money
    {"kind": "DRIVER_CREDIT", "amount_poysha": -7200},               # a "credit" that takes money
    {"amount_poysha": 0},
    {"kind": "REFUND"},                                              # not a kind
    {"ride_id": None},                                               # ride money must name its ride
    {"kind": "TOPUP", "amount_poysha": 500},                         # a top-up tied to a ride
    {"user_id": "no-wallet"},                                        # needs a wallet
])
async def test_transaction_rules(db, bad):
    await add(db, Wallet(user_id=NUSRAT), Wallet(user_id=JASHIM))
    await fails(db, txn(**bad))


async def test_no_double_debit_for_one_ride(db):
    await add(db, Wallet(user_id=NUSRAT), txn())
    await fails(db, txn())


async def test_many_top_ups_are_fine(db):
    await add(db, Wallet(user_id=NUSRAT), *[txn(kind="TOPUP", amount_poysha=1000, ride_id=None) for _ in range(3)])
