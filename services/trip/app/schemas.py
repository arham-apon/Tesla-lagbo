from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

RideStatus = Literal["REQUESTED", "MATCHED", "DRIVER_ARRIVED", "STARTED", "COMPLETED", "CANCELLED"]
ZoneCode = Field(pattern=r"^[A-Z0-9_]{2,30}$")


class RideCreate(BaseModel):
    pickup_zone: str = ZoneCode
    dropoff_zone: str = ZoneCode
    seats: int = Field(ge=1, le=6)
    payment_method: Literal["CASH", "WALLET"] = "CASH"
    quote_id: str | None = None

    @model_validator(mode="after")
    def _distinct(self):
        if self.pickup_zone == self.dropoff_zone:
            raise ValueError("pickup_zone and dropoff_zone must differ")
        return self


class CancelIn(BaseModel):
    reason: str = Field(default="changed_plans", max_length=200)


class DriverBrief(BaseModel):
    driver_id: str
    driver_name: str
    vehicle_nickname: str


class RideOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    status: RideStatus
    pickup_zone: str
    dropoff_zone: str
    seats: int
    pool_id: str | None
    payment_method: str
    estimated_fare_poysha: int
    estimated_pooled_fare_poysha: int
    final_fare_poysha: int | None
    payment_status: str | None
    cancel_reason: str | None
    created_at: datetime
    updated_at: datetime
    driver: DriverBrief | None = None


class HistoryOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    from_status: str | None
    to_status: str
    actor_role: str
    reason: str | None
    at: datetime


class RideDetailOut(RideOut):
    history: list[HistoryOut]


class WaypointOut(BaseModel):
    seq: int
    kind: Literal["PICKUP", "DROPOFF"]
    zone: str
    ride_id: str
    passenger_name: str
    done: bool


class PoolRider(BaseModel):
    ride_id: str
    passenger_name: str
    seats: int
    status: RideStatus
    pickup_zone: str
    dropoff_zone: str


class PoolOut(BaseModel):
    id: str
    status: str
    vehicle_nickname: str
    occupied_seats: int
    max_capacity: int
    riders: list[PoolRider]
    waypoints: list[WaypointOut]


class OfferOut(BaseModel):
    ride_id: str
    passenger_name: str
    pickup_zone: str
    dropoff_zone: str
    seats: int
    estimated_fare_poysha: int
    distance_m: int
    offered_at: datetime
