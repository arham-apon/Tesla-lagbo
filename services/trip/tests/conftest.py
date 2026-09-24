import json
import os
from datetime import datetime
from pathlib import Path

import pytest

SERVICE_DIR = Path(__file__).resolve().parents[1]
INTERNAL_TOKEN = "test-internal-token"

# app.config builds Settings() at import time, so the environment must be set first.
os.environ.update(RABBITMQ_URL="amqp://unused", INTERNAL_TOKEN=INTERNAL_TOKEN, DB_PATH="unused.db")

from alembic import command  # noqa: E402
from alembic.config import Config  # noqa: E402

from sqlalchemy import select  # noqa: E402

from tesla_common.auth import Principal  # noqa: E402
from tesla_common.db import Database  # noqa: E402
from tesla_common.timeutil import new_id  # noqa: E402

from app.clients import Quote  # noqa: E402
from app.models import DriverShift, Outbox, Pool, RideOffer, RideRequest  # noqa: E402
from app.pooling import NEW_RIDE, accept_offer  # noqa: E402

JASHIM, KARIM = "driver-jashim", "driver-karim"
NUSRAT, RAFIQ, SHIRIN = "passenger-nusrat", "passenger-rafiq", "passenger-shirin"


def alembic_config(db_path: Path) -> Config:
    cfg = Config(str(SERVICE_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(SERVICE_DIR / "migrations"))
    cfg.set_main_option("sqlalchemy.url", f"sqlite+aiosqlite:///{db_path.as_posix()}")
    return cfg


@pytest.fixture
def db_path(tmp_path) -> Path:
    """A fresh trip.db, migrated to head with the real Alembic migrations."""
    path = tmp_path / "trip.db"
    command.upgrade(alembic_config(path), "head")
    return path


@pytest.fixture
async def db(db_path):
    database = Database(str(db_path))
    yield database
    await database.dispose()


def shift(driver_id: str = JASHIM, **kw) -> DriverShift:
    return DriverShift(**{"driver_id": driver_id, "driver_name": "Jashim", "vehicle_id": f"vehicle-{driver_id}",
                          "vehicle_nickname": "Bullet", "seat_capacity": 4, "is_online": True} | kw)


# Ids are set up front (the model default only fills them in at insert), so children can point at them.
def pool(driver_id: str = JASHIM, **kw) -> Pool:
    return Pool(**{"id": new_id(), "driver_id": driver_id, "vehicle_id": f"vehicle-{driver_id}",
                   "vehicle_nickname": "Bullet", "driver_name": "Jashim", "max_capacity": 4,
                   "pickup_zone": "BANANI"} | kw)


def ride(passenger_id: str = NUSRAT, **kw) -> RideRequest:
    return RideRequest(**{"id": new_id(), "passenger_id": passenger_id, "passenger_name": "Nusrat", "seats": 1,
                          "pickup_zone": "BANANI", "dropoff_zone": "MOHAKHALI", "payment_method": "CASH",
                          "quote_id": "quote-1", "solo_distance_m": 3500, "estimated_fare_poysha": 11000,
                          "estimated_pooled_fare_poysha": 8800} | kw)


async def add(db: Database, *rows) -> None:
    async with db.rw.begin() as s:
        for r in rows:
            s.add(r)
            await s.flush()  # insert in the given order, so parents exist before children


# ---- 5.5: principals, the Bullet story, fakes for Fare and Matching ----------------------------------------------

AS_NUSRAT = Principal(user_id=NUSRAT, role="PASSENGER", name="Nusrat")
AS_RAFIQ = Principal(user_id=RAFIQ, role="PASSENGER", name="Rafiq")
AS_SHIRIN = Principal(user_id=SHIRIN, role="PASSENGER", name="Shirin")
AS_JASHIM = Principal(user_id=JASHIM, role="DRIVER", name="Jashim")
AS_KARIM = Principal(user_id=KARIM, role="DRIVER", name="Karim")


async def events(db) -> list[tuple[str, dict]]:
    """Everything written to the outbox so far, oldest first: (routing_key, data)."""
    async with db.ro() as s:
        rows = (await s.execute(select(Outbox).order_by(Outbox.id))).scalars().all()
    return [(r.routing_key, json.loads(r.payload)["data"]) for r in rows]


async def bullet_with_nusrat(db) -> tuple[str, str]:
    """The story's start, through the real code: Nusrat (Banani -> Mohakhali, 1 seat) is offered to Jashim,
    who accepts. Returns (pool_id, nusrat_ride_id). Bullet: 4 seats, 1 taken, FORMING."""
    r = ride()
    await add(db, shift(), r, RideOffer(ride_id=r.id, driver_id=JASHIM, distance_m=800))
    return await accept_offer(db.rw, AS_JASHIM, r.id), r.id


def rafiq_plan(nusrat_ride_id: str) -> list[dict]:
    """Matching's answer for Rafiq (Banani -> Gulshan 1) joining Bullet: G1 before M (plan 4.4's worked example)."""
    return [{"ride_id": nusrat_ride_id, "kind": "PICKUP", "zone": "BANANI", "done": False},
            {"ride_id": NEW_RIDE, "kind": "PICKUP", "zone": "BANANI", "done": False},
            {"ride_id": NEW_RIDE, "kind": "DROPOFF", "zone": "GULSHAN_1", "done": False},
            {"ride_id": nusrat_ride_id, "kind": "DROPOFF", "zone": "MOHAKHALI", "done": False}]


class FakeFare:
    """Stands in for FareClient: a quote with the plan's Banani -> Mohakhali numbers."""
    def __init__(self):
        self.calls = 0

    async def quote_for(self, passenger_id, pickup, dropoff, seats, quote_id, request_id) -> Quote:
        self.calls += 1
        return Quote(quote_id=quote_id or f"quote-{self.calls}", passenger_id=passenger_id, pickup_zone=pickup,
                     dropoff_zone=dropoff, seats=seats, distance_m=3500, solo_total_poysha=11000,
                     pooled_total_poysha=8800, expires_at=datetime(2099, 1, 1))


class FakeMatching:
    """Stands in for MatchingClient. Offers every pool in the snapshot (new pickup after the existing ones, new
    drop-off first), unless `stale` says to lie about the version, and returns `candidates` as nearby drivers."""
    def __init__(self, candidates=(), stale_times: int = 0):
        self.candidates, self.stale_times, self.payloads = list(candidates), stale_times, []

    async def evaluate(self, payload: dict, request_id) -> dict:
        self.payloads.append(payload)
        stale = len(self.payloads) <= self.stale_times
        options = []
        for p in payload["open_pools"]:
            pickups = [st for st in p["stops"] if st["kind"] == "PICKUP"]
            drops = [st for st in p["stops"] if st["kind"] == "DROPOFF"]
            plan = pickups + [{"ride_id": NEW_RIDE, "kind": "PICKUP", "zone": payload["pickup_zone"]},
                              {"ride_id": NEW_RIDE, "kind": "DROPOFF", "zone": payload["dropoff_zone"]}] + drops
            options.append({"pool_id": p["pool_id"], "version": p["version"] - 1 if stale else p["version"],
                            "total_route_m": 0, "added_route_m": 0, "max_detour_pct": 100, "plan": plan})
        return {"solo_distance_m": 3500, "compatible_pools": options,
                "candidate_drivers": [{"driver_id": d, "distance_m": m} for d, m in self.candidates]}
