from functools import partial

from sqlalchemy import select, update
from sqlalchemy.dialects.sqlite import insert

from tesla_common.events import emit, first_time

from .models import Fare, Outbox, ProcessedEvent, Quote, Tariff, Wallet, WalletTransaction
from .pricing import compute

PRODUCER = "fare-service"
COMPLETED, CANCELLED = "trip.ride.completed", "trip.ride.cancelled"


async def ensure_wallet(s, user_id: str) -> None:
    await s.execute(insert(Wallet).values(user_id=user_id, balance_poysha=0)
                    .on_conflict_do_nothing(index_elements=[Wallet.user_id]))


async def handle(rw, env: dict) -> None:
    d = env["data"]
    async with rw.begin() as s:
        if not await first_time(s, ProcessedEvent, env["event_id"]):
            return
        if env["event_type"] == CANCELLED:
            await s.execute(update(Quote).where(Quote.id == d["quote_id"]).values(voided=True))
            return
        if env["event_type"] != COMPLETED:
            return  # only these two are bound (start below); nothing else may be read as "completed"

        if await s.scalar(select(Fare.id).where(Fare.ride_id == d["ride_id"])):
            return
        quote = await s.get(Quote, d["quote_id"])
        if quote is None:
            # Raise (so the bus retries, then dead-letters) with a message that says what's wrong.
            raise LookupError(f"quote {d['quote_id']} for ride {d['ride_id']} not found")
        tariff = await s.get(Tariff, quote.tariff_id)
        b = compute(tariff, quote.distance_m, d["seats"], d["pooled"])

        status = "PAID"
        if d["payment_method"] == "WALLET" and b.total_poysha > 0:  # a free ride moves no money (amount <> 0 rule)
            res = await s.execute(update(Wallet)
                                  .where(Wallet.user_id == d["passenger_id"], Wallet.balance_poysha >= b.total_poysha)
                                  .values(balance_poysha=Wallet.balance_poysha - b.total_poysha))
            if res.rowcount == 1:
                s.add(WalletTransaction(user_id=d["passenger_id"], ride_id=d["ride_id"], kind="RIDE_DEBIT",
                                        amount_poysha=-b.total_poysha))
                await ensure_wallet(s, d["driver_id"])
                await s.execute(update(Wallet).where(Wallet.user_id == d["driver_id"])
                                .values(balance_poysha=Wallet.balance_poysha + b.total_poysha))
                s.add(WalletTransaction(user_id=d["driver_id"], ride_id=d["ride_id"], kind="DRIVER_CREDIT",
                                        amount_poysha=b.total_poysha))
            else:
                status = "FAILED"

        fare = Fare(ride_id=d["ride_id"], passenger_id=d["passenger_id"], driver_id=d["driver_id"],
                    quote_id=quote.id, seats=d["seats"], pooled=d["pooled"], base_poysha=b.base_poysha,
                    distance_charge_poysha=b.distance_charge_poysha, pool_discount_poysha=b.pool_discount_poysha,
                    total_poysha=b.total_poysha, payment_method=d["payment_method"], payment_status=status)
        s.add(fare)
        await s.flush()
        emit(s, Outbox, PRODUCER, "fare.ride.settled", {
            "fare_id": fare.id, "ride_id": fare.ride_id, "passenger_id": fare.passenger_id,
            "driver_id": fare.driver_id, "base_poysha": fare.base_poysha,
            "distance_charge_poysha": fare.distance_charge_poysha,
            "pool_discount_poysha": fare.pool_discount_poysha, "total_poysha": fare.total_poysha,
            "payment_method": fare.payment_method, "payment_status": fare.payment_status})


async def start(bus, rw) -> None:
    await bus.consume("fare.ride-lifecycle", [COMPLETED, CANCELLED], partial(handle, rw))
