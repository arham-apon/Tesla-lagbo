from datetime import datetime

from sqlalchemy import Boolean, CheckConstraint, DateTime, ForeignKey, Index, Integer, String, UniqueConstraint, text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from tesla_common.events import OutboxMixin, ProcessedEventMixin
from tesla_common.timeutil import new_id, utcnow


class Base(DeclarativeBase):
    pass


class Tariff(Base):
    __tablename__ = "tariffs"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    base_poysha: Mapped[int] = mapped_column(Integer)
    per_km_poysha: Mapped[int] = mapped_column(Integer)
    pool_discount_pct: Mapped[int] = mapped_column(Integer)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    __table_args__ = (
        CheckConstraint("base_poysha >= 0 AND per_km_poysha >= 0", name="ck_tariff_nonneg"),
        CheckConstraint("pool_discount_pct BETWEEN 0 AND 100", name="ck_tariff_pct"),
        # Added: estimates use "the active tariff", so there must never be two.
        Index("uq_tariff_one_active", "active", unique=True, sqlite_where=text("active = 1")),
    )


class Quote(Base):
    __tablename__ = "quotes"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    passenger_id: Mapped[str] = mapped_column(String(36), index=True)
    pickup_zone: Mapped[str] = mapped_column(String(30))
    dropoff_zone: Mapped[str] = mapped_column(String(30))
    seats: Mapped[int] = mapped_column(Integer)
    distance_m: Mapped[int] = mapped_column(Integer)
    tariff_id: Mapped[int] = mapped_column(ForeignKey("tariffs.id"))
    solo_total_poysha: Mapped[int] = mapped_column(Integer)
    pooled_total_poysha: Mapped[int] = mapped_column(Integer)
    voided: Mapped[bool] = mapped_column(Boolean, default=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    __table_args__ = (
        CheckConstraint("seats BETWEEN 1 AND 6", name="ck_quote_seats"),
        # Added: Matching says 0 m for the same zone, which would quote the bare base fare (found in 6.2).
        CheckConstraint("pickup_zone <> dropoff_zone", name="ck_quote_zones"),
        CheckConstraint("distance_m > 0", name="ck_quote_distance"),
        CheckConstraint("pooled_total_poysha >= 0 AND pooled_total_poysha <= solo_total_poysha",
                        name="ck_quote_totals"),
    )


class Fare(Base):
    __tablename__ = "fares"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    ride_id: Mapped[str] = mapped_column(String(36), unique=True)
    passenger_id: Mapped[str] = mapped_column(String(36))
    driver_id: Mapped[str] = mapped_column(String(36))
    quote_id: Mapped[str] = mapped_column(ForeignKey("quotes.id"))
    seats: Mapped[int] = mapped_column(Integer)
    pooled: Mapped[bool] = mapped_column(Boolean)
    base_poysha: Mapped[int] = mapped_column(Integer)
    distance_charge_poysha: Mapped[int] = mapped_column(Integer)
    pool_discount_poysha: Mapped[int] = mapped_column(Integer)
    total_poysha: Mapped[int] = mapped_column(Integer)
    payment_method: Mapped[str] = mapped_column(String(10))
    payment_status: Mapped[str] = mapped_column(String(10))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    __table_args__ = (
        CheckConstraint("total_poysha = base_poysha + distance_charge_poysha - pool_discount_poysha",
                        name="ck_fare_arithmetic"),
        CheckConstraint("total_poysha >= 0", name="ck_fare_nonneg"),
        # Added: the ledger can't hold negative parts, impossible seat counts, or a pool discount on a solo ride.
        CheckConstraint("base_poysha >= 0 AND distance_charge_poysha >= 0 AND pool_discount_poysha >= 0",
                        name="ck_fare_parts_nonneg"),
        CheckConstraint("seats BETWEEN 1 AND 6", name="ck_fare_seats"),
        CheckConstraint("pooled OR pool_discount_poysha = 0", name="ck_fare_discount_only_pooled"),
        CheckConstraint("payment_method IN ('CASH','WALLET')", name="ck_fare_method"),
        CheckConstraint("payment_status IN ('PAID','FAILED','REFUNDED')", name="ck_fare_status"),
        Index("ix_fare_passenger", "passenger_id", "created_at"),
        Index("ix_fare_driver", "driver_id", "created_at"),
    )


class Wallet(Base):
    __tablename__ = "wallets"
    user_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    balance_poysha: Mapped[int] = mapped_column(Integer, default=0)
    __table_args__ = (CheckConstraint("balance_poysha >= 0", name="ck_wallet_nonneg"),)


class WalletTransaction(Base):
    __tablename__ = "wallet_transactions"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    user_id: Mapped[str] = mapped_column(ForeignKey("wallets.user_id"))
    ride_id: Mapped[str | None] = mapped_column(String(36))
    kind: Mapped[str] = mapped_column(String(15))
    amount_poysha: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    __table_args__ = (
        CheckConstraint("kind IN ('TOPUP','RIDE_DEBIT','DRIVER_CREDIT')", name="ck_wtx_kind"),
        CheckConstraint("amount_poysha <> 0", name="ck_wtx_nonzero"),
        # Added: money moves the right way (a debit takes, a top-up or credit gives), and ride money names its ride.
        CheckConstraint("(kind = 'RIDE_DEBIT' AND amount_poysha < 0) OR "
                        "(kind <> 'RIDE_DEBIT' AND amount_poysha > 0)", name="ck_wtx_sign"),
        CheckConstraint("(kind = 'TOPUP' AND ride_id IS NULL) OR (kind <> 'TOPUP' AND ride_id IS NOT NULL)",
                        name="ck_wtx_ride"),
        UniqueConstraint("ride_id", "kind", "user_id", name="uq_wtx_ride_kind_user"),
        Index("ix_wtx_user", "user_id", "created_at"),
    )


class Outbox(OutboxMixin, Base):
    pass


class ProcessedEvent(ProcessedEventMixin, Base):
    pass
