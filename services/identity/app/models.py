from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Integer, String
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from tesla_common.events import OutboxMixin
from tesla_common.timeutil import new_id, utcnow


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    full_name: Mapped[str] = mapped_column(String(100))
    phone: Mapped[str] = mapped_column(String(20), unique=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    role: Mapped[str] = mapped_column(String(20))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    __table_args__ = (CheckConstraint("role IN ('PASSENGER','DRIVER','ADMIN')", name="ck_user_role"),)


class Driver(Base):
    __tablename__ = "drivers"
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"), primary_key=True)
    license_number: Mapped[str] = mapped_column(String(50), unique=True)
    status: Mapped[str] = mapped_column(String(10), default="OFFLINE")
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)
    vehicle: Mapped["Vehicle | None"] = relationship(back_populates="driver", uselist=False, lazy="selectin")
    # Tells the unit of work to INSERT users before drivers when both are added in one transaction
    # (register does this). lazy="raise": never loads implicitly; query User explicitly instead.
    user: Mapped[User] = relationship(lazy="raise")
    __table_args__ = (CheckConstraint("status IN ('OFFLINE','ONLINE')", name="ck_driver_status"),)


class Vehicle(Base):
    __tablename__ = "vehicles"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    driver_id: Mapped[str] = mapped_column(ForeignKey("drivers.user_id", ondelete="RESTRICT"), unique=True)
    nickname: Mapped[str] = mapped_column(String(50))
    make: Mapped[str] = mapped_column(String(50))
    model: Mapped[str] = mapped_column(String(50))
    plate: Mapped[str] = mapped_column(String(30), unique=True)
    seat_capacity: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    driver: Mapped[Driver] = relationship(back_populates="vehicle")
    __table_args__ = (CheckConstraint("seat_capacity BETWEEN 1 AND 6", name="ck_vehicle_capacity"),)


class Outbox(OutboxMixin, Base):
    pass
