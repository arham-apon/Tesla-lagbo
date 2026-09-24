from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

ZoneCode = Field(pattern=r"^[A-Z0-9_]{2,30}$")  # same shape as Trip's RideCreate


class EstimateIn(BaseModel):
    pickup_zone: str = ZoneCode
    dropoff_zone: str = ZoneCode
    seats: int = Field(ge=1, le=6)

    @model_validator(mode="after")
    def _distinct(self):
        # Matching says 0 m for the same zone, which would quote the bare base fare (found in 6.2).
        if self.pickup_zone == self.dropoff_zone:
            raise ValueError("pickup_zone and dropoff_zone must differ")
        return self


class InternalQuoteIn(EstimateIn):
    """Trip asks on the passenger's behalf (plan 6.5: "same as estimate, passenger_id in body")."""
    passenger_id: str = Field(min_length=1, max_length=36)


class BreakdownOut(BaseModel):
    base_poysha: int
    distance_charge_poysha: int
    pool_discount_poysha: int
    total_poysha: int


class QuoteOut(BaseModel):
    quote_id: str
    passenger_id: str
    pickup_zone: str
    dropoff_zone: str
    seats: int
    distance_m: int
    solo_total_poysha: int
    pooled_total_poysha: int
    solo: BreakdownOut
    pooled: BreakdownOut
    expires_at: datetime


class FareOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    ride_id: str
    quote_id: str
    seats: int
    pooled: bool
    base_poysha: int
    distance_charge_poysha: int
    pool_discount_poysha: int
    total_poysha: int
    payment_method: Literal["CASH", "WALLET"]
    payment_status: Literal["PAID", "FAILED", "REFUNDED"]
    created_at: datetime


class TopUpIn(BaseModel):
    amount_poysha: int = Field(ge=1, le=500_000)  # plan 6.5: 1 <= amount <= 500000 (5,000 taka)


class TransactionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    kind: str
    amount_poysha: int
    ride_id: str | None
    created_at: datetime


class WalletOut(BaseModel):
    balance_poysha: int
    transactions: list[TransactionOut]


class BalanceOut(BaseModel):
    balance_poysha: int


class EarningsOut(BaseModel):
    rides: int
    total_poysha: int
    cash_poysha: int
    wallet_poysha: int
