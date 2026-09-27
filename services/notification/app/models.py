from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, Index, Integer, String, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from tesla_common.events import ProcessedEventMixin
from tesla_common.timeutil import utcnow


class Base(DeclarativeBase):
    pass


class Notification(Base):
    """One message saved for one person: the inbox a reconnecting phone catches up from (7.1)."""
    __tablename__ = "notifications"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[str] = mapped_column(String(36))
    type: Mapped[str] = mapped_column(String(40))
    payload: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    read_at: Mapped[datetime | None] = mapped_column(DateTime)
    __table_args__ = (
        Index("ix_notif_user_id", "user_id", "id"),
        # Added: the inbox API hands payload back as JSON; a row that isn't JSON would break every catch-up
        # for that person, so the database refuses it at write time instead.
        CheckConstraint("json_valid(payload)", name="ck_notif_payload_json"),
        CheckConstraint("length(user_id) > 0", name="ck_notif_user"),
    )


class ProcessedEvent(ProcessedEventMixin, Base):
    pass
