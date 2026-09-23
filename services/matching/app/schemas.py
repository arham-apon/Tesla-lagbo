from typing import Literal

from pydantic import BaseModel, Field, model_validator

NEW_RIDE = "__new__"


class Stop(BaseModel):
    ride_id: str
    kind: Literal["PICKUP", "DROPOFF"]
    zone: str
    done: bool = False


class OpenPool(BaseModel):
    pool_id: str
    driver_id: str
    pickup_zone: str
    remaining_seats: int = Field(ge=0)
    version: int
    stops: list[Stop]


class EvaluateIn(BaseModel):
    pickup_zone: str
    dropoff_zone: str
    seats: int = Field(ge=1, le=6)
    open_pools: list[OpenPool] = []
    max_candidates: int = Field(default=5, ge=1, le=20)

    @model_validator(mode="after")
    def _distinct(self):
        # Same rule as Trip's RideCreate. A 0 m solo distance would divide by zero in the planner.
        if self.pickup_zone == self.dropoff_zone:
            raise ValueError("pickup_zone and dropoff_zone must differ")
        return self


class PoolOption(BaseModel):
    pool_id: str
    version: int
    total_route_m: int
    added_route_m: int
    max_detour_pct: int
    plan: list[Stop]


class CandidateDriver(BaseModel):
    driver_id: str
    distance_m: int


class EvaluateOut(BaseModel):
    solo_distance_m: int
    compatible_pools: list[PoolOption]
    candidate_drivers: list[CandidateDriver]


class LocationPing(BaseModel):
    lat: float = Field(ge=23.60, le=23.95)
    lng: float = Field(ge=90.30, le=90.55)
    heading: int | None = Field(default=None, ge=0, le=359)
