from datetime import datetime

from sqlalchemy import (Boolean, CheckConstraint, DateTime, ForeignKey, Index, Integer, String,
                        UniqueConstraint, text)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from tesla_common.events import OutboxMixin, ProcessedEventMixin
from tesla_common.timeutil import new_id, utcnow

ACTIVE_RIDE_STATUSES = ("REQUESTED", "MATCHED", "DRIVER_ARRIVED", "STARTED")
LIVE_POOL_STATUSES = ("FORMING", "IN_PROGRESS")


class Base(DeclarativeBase):
    pass


class DriverShift(Base):
    __tablename__ = "driver_shifts"
    driver_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    driver_name: Mapped[str] = mapped_column(String(100))
    vehicle_id: Mapped[str] = mapped_column(String(36))
    vehicle_nickname: Mapped[str] = mapped_column(String(50))
    seat_capacity: Mapped[int] = mapped_column(Integer)
    is_online: Mapped[bool] = mapped_column(Boolean, default=False)
    state_ts: Mapped[str] = mapped_column(String(40), default="")
    __table_args__ = (CheckConstraint("seat_capacity BETWEEN 1 AND 6", name="ck_shift_capacity"),)


class Pool(Base):
    __tablename__ = "pools"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    driver_id: Mapped[str] = mapped_column(ForeignKey("driver_shifts.driver_id", ondelete="RESTRICT"))
    vehicle_id: Mapped[str] = mapped_column(String(36))
    vehicle_nickname: Mapped[str] = mapped_column(String(50))
    driver_name: Mapped[str] = mapped_column(String(100))
    max_capacity: Mapped[int] = mapped_column(Integer)
    occupied_seats: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(15), default="FORMING")
    pickup_zone: Mapped[str] = mapped_column(String(30))
    version: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    started_at: Mapped[datetime | None] = mapped_column(DateTime)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime)
    __table_args__ = (
        CheckConstraint("max_capacity BETWEEN 1 AND 6", name="ck_pool_max"),
        CheckConstraint("occupied_seats >= 0 AND occupied_seats <= max_capacity", name="ck_pool_capacity"),
        CheckConstraint("status IN ('FORMING','IN_PROGRESS','COMPLETED','CANCELLED')", name="ck_pool_status"),
        Index("uq_pool_one_live_per_driver", "driver_id", unique=True,
              sqlite_where=text("status IN ('FORMING','IN_PROGRESS')")),
        Index("ix_pool_matching", "status", "pickup_zone"),
    )


class RideRequest(Base):
    __tablename__ = "ride_requests"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    passenger_id: Mapped[str] = mapped_column(String(36))
    passenger_name: Mapped[str] = mapped_column(String(100))
    pool_id: Mapped[str | None] = mapped_column(ForeignKey("pools.id", ondelete="RESTRICT"))
    seats: Mapped[int] = mapped_column(Integer)
    pickup_zone: Mapped[str] = mapped_column(String(30))
    dropoff_zone: Mapped[str] = mapped_column(String(30))
    status: Mapped[str] = mapped_column(String(20), default="REQUESTED")
    payment_method: Mapped[str] = mapped_column(String(10))
    quote_id: Mapped[str] = mapped_column(String(36))
    solo_distance_m: Mapped[int] = mapped_column(Integer)
    estimated_fare_poysha: Mapped[int] = mapped_column(Integer)
    estimated_pooled_fare_poysha: Mapped[int] = mapped_column(Integer)
    final_fare_poysha: Mapped[int | None] = mapped_column(Integer)
    payment_status: Mapped[str | None] = mapped_column(String(10))
    cancel_reason: Mapped[str | None] = mapped_column(String(200))
    version: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)
    __table_args__ = (
        CheckConstraint("seats BETWEEN 1 AND 6", name="ck_ride_seats"),
        CheckConstraint("pickup_zone <> dropoff_zone", name="ck_ride_zones"),
        CheckConstraint("status IN ('REQUESTED','MATCHED','DRIVER_ARRIVED','STARTED','COMPLETED','CANCELLED')",
                        name="ck_ride_status"),
        CheckConstraint("payment_method IN ('CASH','WALLET')", name="ck_ride_payment"),
        CheckConstraint("estimated_fare_poysha >= 0", name="ck_ride_fare"),
        CheckConstraint("status = 'REQUESTED' OR status = 'CANCELLED' OR pool_id IS NOT NULL",
                        name="ck_ride_pool_when_matched"),
        Index("uq_ride_one_active_per_passenger", "passenger_id", unique=True,
              sqlite_where=text("status IN ('REQUESTED','MATCHED','DRIVER_ARRIVED','STARTED')")),
        Index("ix_ride_passenger_created", "passenger_id", "created_at"),
        Index("ix_ride_status_created", "status", "created_at"),
        Index("ix_ride_pool", "pool_id"),
    )


class PoolWaypoint(Base):
    __tablename__ = "pool_waypoints"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    pool_id: Mapped[str] = mapped_column(ForeignKey("pools.id", ondelete="CASCADE"))
    ride_request_id: Mapped[str] = mapped_column(ForeignKey("ride_requests.id", ondelete="CASCADE"))
    seq: Mapped[int] = mapped_column(Integer)
    kind: Mapped[str] = mapped_column(String(10))
    zone: Mapped[str] = mapped_column(String(30))
    done_at: Mapped[datetime | None] = mapped_column(DateTime)
    __table_args__ = (
        UniqueConstraint("pool_id", "seq", name="uq_waypoint_seq"),
        CheckConstraint("seq > 0", name="ck_waypoint_seq"),
        CheckConstraint("kind IN ('PICKUP','DROPOFF')", name="ck_waypoint_kind"),
    )


class RideStatusHistory(Base):
    __tablename__ = "ride_status_history"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ride_id: Mapped[str] = mapped_column(ForeignKey("ride_requests.id", ondelete="CASCADE"))
    from_status: Mapped[str | None] = mapped_column(String(20))
    to_status: Mapped[str] = mapped_column(String(20))
    actor_id: Mapped[str] = mapped_column(String(36))
    actor_role: Mapped[str] = mapped_column(String(10))
    reason: Mapped[str | None] = mapped_column(String(200))
    at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    __table_args__ = (
        # Not in the plan: the audit must say who did it, so only the three real actors are accepted.
        CheckConstraint("actor_role IN ('PASSENGER','DRIVER','SYSTEM')", name="ck_history_actor"),
        Index("ix_history_ride", "ride_id", "id"),
    )


class RideOffer(Base):
    __tablename__ = "ride_offers"
    ride_id: Mapped[str] = mapped_column(ForeignKey("ride_requests.id", ondelete="CASCADE"), primary_key=True)
    driver_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    status: Mapped[str] = mapped_column(String(10), default="OFFERED")
    distance_m: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    __table_args__ = (
        CheckConstraint("status IN ('OFFERED','ACCEPTED','DECLINED')", name="ck_offer_status"),
        Index("ix_offer_driver", "driver_id", "status"),
    )


class Outbox(OutboxMixin, Base):
    pass


class ProcessedEvent(ProcessedEventMixin, Base):
    pass
