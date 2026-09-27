from functools import partial

from sqlalchemy import update

from tesla_common.events import first_time

from .models import DriverShift, ProcessedEvent, RideRequest


ONLINE, OFFLINE = "identity.driver.online", "identity.driver.offline"


async def on_driver_shift(rw, env: dict) -> None:
    d, ts, kind = env["data"], env["occurred_at"], env["event_type"]
    async with rw.begin() as s:
        if not await first_time(s, ProcessedEvent, env["event_id"]):
            return
        if kind not in (ONLINE, OFFLINE):
            # The queue takes identity.driver.*; a future event type must not be read as "went offline".
            return
        shift = await s.get(DriverShift, d["driver_id"])
        # Text comparison is safe: emit() writes occurred_at with a fixed width (6 decimals, since Part 5).
        if shift and shift.state_ts and shift.state_ts >= ts:
            return
        if kind == ONLINE:
            if shift is None:
                shift = DriverShift(driver_id=d["driver_id"])
                s.add(shift)
            shift.driver_name = d["driver_name"]
            shift.vehicle_id = d["vehicle_id"]
            shift.vehicle_nickname = d["vehicle_nickname"]
            shift.seat_capacity = d["seat_capacity"]
            shift.is_online = True
        elif shift is not None:
            shift.is_online = False
        if shift is not None:
            shift.state_ts = ts


async def on_fare_settled(rw, env: dict) -> None:
    d = env["data"]
    async with rw.begin() as s:
        if not await first_time(s, ProcessedEvent, env["event_id"]):
            return
        await s.execute(update(RideRequest).where(RideRequest.id == d["ride_id"])
                        .values(final_fare_poysha=d["total_poysha"], payment_status=d["payment_status"]))


async def start(bus, rw) -> None:
    await bus.consume("trip.driver-shift", ["identity.driver.*"], partial(on_driver_shift, rw))
    await bus.consume("trip.fare-settled", ["fare.ride.settled"], partial(on_fare_settled, rw))
