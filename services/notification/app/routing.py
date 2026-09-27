"""Who hears about what (plan 7.4). The privacy boundary for everything pushed to phones.

Change from the plan: the plan forwarded each event's whole `data` to its recipients. Here every recipient gets
an ALLOWLIST of named fields, so a field Trip or Fare adds to an event later (say, a list of co-riders) can never
reach a phone by accident. A pure function: no database, sockets or Redis, so it can be tested exhaustively.
"""

# (message type, fields) per recipient. Anything not named here is never sent.
OFFER = ("ride_id", "pickup_zone", "dropoff_zone", "seats", "estimated_fare_poysha")
MATCHED_PASSENGER = ("ride_id", "pool_id", "driver_name", "vehicle_nickname", "seats")   # "driver name + Bullet"
MATCHED_DRIVER = ("ride_id", "pool_id", "seats", "joined_existing_pool")
STATUS = ("ride_id", "pool_id", "from_status", "to_status", "actor_role")
CANCELLED = ("ride_id", "pool_id", "from_status", "cancelled_by", "reason")
FARE_PASSENGER = ("ride_id", "fare_id", "base_poysha", "distance_charge_poysha", "pool_discount_poysha",
                  "total_poysha", "payment_method", "payment_status")                    # her own fare, in full
FARE_DRIVER = ("ride_id", "total_poysha", "payment_method", "payment_status")             # enough to collect cash
POOL = ("pool_id", "status", "occupied_seats", "max_capacity", "member_passenger_ids", "waypoints")


def _pick(d: dict, fields: tuple[str, ...]) -> dict:
    return {k: d[k] for k in fields if k in d}


def recipients(env: dict) -> list[tuple[str, dict]]:
    t, d = env["event_type"], env["data"]

    def msg(kind: str, fields: tuple[str, ...]) -> dict:
        return {"type": kind, "event_id": env["event_id"], "at": env["occurred_at"], "data": _pick(d, fields)}

    if t == "trip.ride.requested":
        return [(drv, msg("ride.offer", OFFER)) for drv in d["candidate_driver_ids"]]
    if t == "trip.ride.matched":
        return [(d["passenger_id"], msg("ride.matched", MATCHED_PASSENGER)),
                (d["driver_id"], msg("ride.matched", MATCHED_DRIVER))]
    if t in ("trip.ride.status_changed", "trip.ride.cancelled"):
        kind, fields = ("ride.status", STATUS) if t == "trip.ride.status_changed" else ("ride.cancelled", CANCELLED)
        out = [(d["passenger_id"], msg(kind, fields))]
        if d.get("driver_id"):  # a ride cancelled before anyone accepted it has no driver
            out.append((d["driver_id"], msg(kind, fields)))
        return out
    if t == "trip.pool.updated":
        return [(d["driver_id"], msg("pool.updated", POOL))]  # the driver only: passengers never get co-rider data
    if t == "fare.ride.settled":
        return [(d["passenger_id"], msg("fare.settled", FARE_PASSENGER)),
                (d["driver_id"], msg("fare.settled", FARE_DRIVER))]
    return []  # e.g. trip.ride.completed: it's bound (trip.ride.*) but Fare's fare.ride.settled says it better
