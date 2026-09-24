"""6.6 / 6.7.4: settling a ride from Trip's events: the fare row, the wallet movements, and fare.ride.settled."""
import asyncio
import json
from datetime import datetime, timedelta
from unittest.mock import patch

import pytest
from sqlalchemy import func, select

from tesla_common.events import emit
from tesla_common.timeutil import utcnow

from app import settlement
from app.models import Fare, Outbox, ProcessedEvent, Quote, Tariff, Wallet, WalletTransaction
from app.pricing import compute
from conftest import JASHIM, NUSRAT, RAFIQ, add

V1 = Tariff(id=1, base_poysha=3000, per_km_poysha=1500, pool_discount_pct=20)
SETTLED_FIELDS = {"fare_id", "ride_id", "passenger_id", "driver_id", "base_poysha", "distance_charge_poysha",
                  "pool_discount_poysha", "total_poysha", "payment_method", "payment_status"}  # registry (0.4)


class _Capture:
    def __init__(self):
        self.rows = []

    def add(self, row):
        self.rows.append(row)


def event(routing_key: str, data: dict, at: str = "2026-09-24T09:05:00") -> dict:
    """An envelope made by the real tesla_common emit(), as Trip's outbox would send it."""
    session = _Capture()
    with patch("tesla_common.events.utcnow", return_value=datetime.fromisoformat(at)):
        emit(session, lambda **kw: kw, "trip-service", routing_key, data)
    return json.loads(session.rows[0]["payload"])


def completed(ride_id="ride-nusrat", quote_id="q-nusrat", passenger=NUSRAT, seats=1, pooled=True,
              method="WALLET") -> dict:
    """Trip's trip.ride.completed, with exactly the registry's fields (Trip's contract test, 5.5)."""
    return event("trip.ride.completed", {
        "ride_id": ride_id, "pool_id": "pool-bullet", "passenger_id": passenger, "driver_id": JASHIM,
        "quote_id": quote_id, "seats": seats, "payment_method": method, "pooled": pooled,
        "co_rider_count": 1 if pooled else 0})


def cancelled(quote_id="q-nusrat", ride_id="ride-nusrat") -> dict:
    return event("trip.ride.cancelled", {
        "ride_id": ride_id, "pool_id": None, "passenger_id": NUSRAT, "driver_id": None, "from_status": "REQUESTED",
        "cancelled_by": "PASSENGER", "reason": "changed_plans", "quote_id": quote_id})


def quote(qid="q-nusrat", passenger=NUSRAT, distance=3500, seats=1, dropoff="MOHAKHALI", tariff=V1) -> Quote:
    return Quote(id=qid, passenger_id=passenger, pickup_zone="BANANI", dropoff_zone=dropoff, seats=seats,
                 distance_m=distance, tariff_id=tariff.id,
                 solo_total_poysha=compute(tariff, distance, seats, False).total_poysha,
                 pooled_total_poysha=compute(tariff, distance, seats, True).total_poysha,
                 expires_at=utcnow() + timedelta(minutes=10))


async def wallet(db, user_id) -> int | None:
    async with db.ro() as s:
        return await s.scalar(select(Wallet.balance_poysha).where(Wallet.user_id == user_id))


async def fares(db) -> list[Fare]:
    async with db.ro() as s:
        return (await s.execute(select(Fare))).scalars().all()


async def txns(db) -> list[tuple]:
    async with db.ro() as s:
        rows = (await s.execute(select(WalletTransaction).order_by(WalletTransaction.created_at))).scalars().all()
    return [(t.user_id, t.kind, t.amount_poysha, t.ride_id) for t in rows]


async def published(db) -> list[tuple[str, dict]]:
    async with db.ro() as s:
        rows = (await s.execute(select(Outbox).order_by(Outbox.id))).scalars().all()
    return [(r.routing_key, json.loads(r.payload)["data"]) for r in rows]


@pytest.fixture
async def nusrat_with_500(db):
    """Nusrat's quote for Banani -> Mohakhali, and 500 taka in her wallet (as seeded)."""
    await add(db, quote(), Wallet(user_id=NUSRAT, balance_poysha=50000),
              WalletTransaction(user_id=NUSRAT, kind="TOPUP", amount_poysha=50000))


# ---- the story --------------------------------------------------------------------------------------------------

async def test_nusrat_pays_72_from_her_wallet(db, nusrat_with_500):
    await settlement.handle(db.rw, completed())
    (f,) = await fares(db)
    assert (f.ride_id, f.pooled, f.base_poysha, f.distance_charge_poysha, f.pool_discount_poysha, f.total_poysha,
            f.payment_method, f.payment_status) == ("ride-nusrat", True, 3000, 5250, 1050, 7200, "WALLET", "PAID")
    assert await wallet(db, NUSRAT) == 50000 - 7200
    assert await wallet(db, JASHIM) == 7200  # his wallet is created on his first credit
    assert (await txns(db))[1:] == [(NUSRAT, "RIDE_DEBIT", -7200, "ride-nusrat"),
                                    (JASHIM, "DRIVER_CREDIT", 7200, "ride-nusrat")]
    ((key, data),) = await published(db)
    assert key == "fare.ride.settled" and set(data) == SETTLED_FIELDS
    assert (data["fare_id"], data["total_poysha"], data["payment_status"]) == (f.id, 7200, "PAID")


async def test_rafiq_pays_54_in_cash(db):
    await add(db, quote("q-rafiq", RAFIQ, 2000, dropoff="GULSHAN_1"))
    await settlement.handle(db.rw, completed("ride-rafiq", "q-rafiq", RAFIQ, method="CASH"))
    (f,) = await fares(db)
    assert (f.total_poysha, f.payment_status) == (5400, "PAID")
    assert await txns(db) == [] and await wallet(db, JASHIM) is None  # cash: no wallet moves at all


async def test_alone_pays_the_solo_price(db, nusrat_with_500):
    await settlement.handle(db.rw, completed(pooled=False))
    (f,) = await fares(db)
    assert (f.pooled, f.pool_discount_poysha, f.total_poysha) == (False, 0, 8250)


async def test_two_seats(db):
    await add(db, quote(seats=2))
    await settlement.handle(db.rw, completed(seats=2, method="CASH"))
    assert (await fares(db))[0].total_poysha == 14400


# ---- wallet short (plan 6.7.4) ----------------------------------------------------------------------------------

async def test_wallet_short_is_failed_and_nothing_moves(db):
    await add(db, quote(), Wallet(user_id=NUSRAT, balance_poysha=7199))  # 1 poysha short
    await settlement.handle(db.rw, completed())
    (f,) = await fares(db)
    assert (f.total_poysha, f.payment_status) == (7200, "FAILED")
    assert await wallet(db, NUSRAT) == 7199 and await wallet(db, JASHIM) is None and await txns(db) == []
    ((_, data),) = await published(db)
    assert data["payment_status"] == "FAILED"  # Notification tells Jashim to collect cash


async def test_exactly_enough_is_paid(db):
    await add(db, quote(), Wallet(user_id=NUSRAT, balance_poysha=7200))
    await settlement.handle(db.rw, completed())
    assert (await fares(db))[0].payment_status == "PAID" and await wallet(db, NUSRAT) == 0


async def test_no_wallet_at_all_is_failed(db):
    await add(db, quote())
    await settlement.handle(db.rw, completed())
    assert (await fares(db))[0].payment_status == "FAILED"


# ---- safe to receive twice: the plan's three layers --------------------------------------------------------------

async def test_same_event_twice_one_fare_one_debit(db, nusrat_with_500):
    ev = completed()
    await settlement.handle(db.rw, ev)
    await settlement.handle(db.rw, ev)  # layer 1: processed_events
    assert len(await fares(db)) == 1 and await wallet(db, NUSRAT) == 42800 and len(await published(db)) == 1


async def test_two_different_events_for_one_ride_one_fare(db, nusrat_with_500):
    await settlement.handle(db.rw, completed())
    await settlement.handle(db.rw, completed())  # a new event id, same ride: layer 2, fares.ride_id
    assert len(await fares(db)) == 1 and await wallet(db, NUSRAT) == 42800 and len(await published(db)) == 1


async def test_racing_events_for_one_ride_still_one_debit(db, nusrat_with_500):
    await asyncio.gather(*(settlement.handle(db.rw, completed()) for _ in range(5)))
    assert len(await fares(db)) == 1 and await wallet(db, NUSRAT) == 42800
    async with db.ro() as s:
        assert await s.scalar(select(func.count()).select_from(WalletTransaction)
                              .where(WalletTransaction.kind == "RIDE_DEBIT")) == 1


# ---- cancelled --------------------------------------------------------------------------------------------------

async def test_cancel_voids_the_quote_and_charges_nothing(db, nusrat_with_500):
    await settlement.handle(db.rw, cancelled())
    async with db.ro() as s:
        assert (await s.get(Quote, "q-nusrat")).voided is True
    assert await fares(db) == [] and await published(db) == [] and await wallet(db, NUSRAT) == 50000


async def test_cancel_twice_is_harmless(db, nusrat_with_500):
    ev = cancelled()
    await settlement.handle(db.rw, ev)
    await settlement.handle(db.rw, ev)
    async with db.ro() as s:
        assert await s.scalar(select(func.count()).select_from(ProcessedEvent)) == 1


# ---- the price is the quote's ------------------------------------------------------------------------------------

async def test_tariff_locked_at_quote_time(db, nusrat_with_500):
    async with db.rw.begin() as s:
        (await s.get(Tariff, 1)).active = False
        s.add(Tariff(id=2, base_poysha=9000, per_km_poysha=9000, pool_discount_pct=0, active=True))
    await settlement.handle(db.rw, completed())
    assert (await fares(db))[0].total_poysha == 7200  # tariff 1's price, as shown to her


async def test_an_expired_quote_still_settles(db, nusrat_with_500):
    async with db.rw.begin() as s:
        (await s.get(Quote, "q-nusrat")).expires_at = utcnow() - timedelta(hours=1)  # a long ride
    await settlement.handle(db.rw, completed())
    assert (await fares(db))[0].total_poysha == 7200


# ---- things that must not happen ---------------------------------------------------------------------------------

async def test_unknown_quote_raises_clearly_and_leaves_nothing(db):
    ev = completed(quote_id="no-such-quote")
    with pytest.raises(LookupError, match="quote no-such-quote for ride ride-nusrat not found"):
        await settlement.handle(db.rw, ev)
    async with db.ro() as s:
        assert await s.get(ProcessedEvent, ev["event_id"]) is None  # rolled back, so the retry really runs
    assert await fares(db) == []


async def test_other_trip_events_are_ignored(db, nusrat_with_500):
    """The fix: the plan read anything that wasn't "cancelled" as "completed"."""
    ev = event("trip.ride.status_changed", {"ride_id": "ride-nusrat", "quote_id": "q-nusrat"})
    await settlement.handle(db.rw, ev)
    assert await fares(db) == [] and await wallet(db, NUSRAT) == 50000


async def test_a_free_wallet_ride_moves_no_money(db):
    """A 100 %-pool-discount, 0-base tariff gives a 0-poysha fare. The plan would write a 0 transaction, which
    the ledger refuses, and the ride would be dead-lettered."""
    free = Tariff(id=2, base_poysha=0, per_km_poysha=1500, pool_discount_pct=100, active=False)
    await add(db, free, quote(tariff=free), Wallet(user_id=NUSRAT, balance_poysha=100))
    await settlement.handle(db.rw, completed())
    (f,) = await fares(db)
    assert (f.total_poysha, f.payment_status) == (0, "PAID") and await txns(db) == []


async def test_money_always_adds_up(db, nusrat_with_500):
    """Every wallet balance equals the sum of its transactions, after a mixed evening."""
    await add(db, quote("q-2", distance=2000, dropoff="GULSHAN_1"), quote("q-3", distance=1000, dropoff="GULSHAN_2"))
    await settlement.handle(db.rw, completed())
    await settlement.handle(db.rw, completed("ride-2", "q-2", pooled=False))
    await settlement.handle(db.rw, completed("ride-3", "q-3", method="CASH"))
    async with db.ro() as s:
        for user_id, balance in (await s.execute(select(Wallet.user_id, Wallet.balance_poysha))).all():
            total = await s.scalar(select(func.coalesce(func.sum(WalletTransaction.amount_poysha), 0))
                                   .where(WalletTransaction.user_id == user_id))
            assert balance == total, user_id
    assert await wallet(db, NUSRAT) == 50000 - 7200 - 6000 and await wallet(db, JASHIM) == 7200 + 6000


# ---- wiring -----------------------------------------------------------------------------------------------------

async def test_queue_and_bindings_match_the_plan(db, nusrat_with_500):
    class RecordingBus:
        queues = {}

        async def consume(self, queue, bindings, handler, **kw):
            self.queues[queue] = (bindings, handler)

    bus = RecordingBus()
    await settlement.start(bus, db.rw)
    ((queue, (bindings, handler)),) = bus.queues.items()
    assert (queue, bindings) == ("fare.ride-lifecycle", ["trip.ride.completed", "trip.ride.cancelled"])
    await handler(completed())
    assert len(await fares(db)) == 1
