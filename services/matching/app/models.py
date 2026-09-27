from sqlalchemy import CheckConstraint, Float, ForeignKey, Integer, String
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

# No processed_events table: Matching's consumer only makes Redis updates that are safe to repeat (plan 4.7).


class Base(DeclarativeBase):
    pass


class Zone(Base):
    __tablename__ = "zones"
    code: Mapped[str] = mapped_column(String(30), primary_key=True)
    name: Mapped[str] = mapped_column(String(60))
    lat: Mapped[float] = mapped_column(Float)
    lng: Mapped[float] = mapped_column(Float)


class ZoneDistance(Base):
    __tablename__ = "zone_distances"
    from_zone: Mapped[str] = mapped_column(ForeignKey("zones.code"), primary_key=True)
    to_zone: Mapped[str] = mapped_column(ForeignKey("zones.code"), primary_key=True)
    distance_m: Mapped[int] = mapped_column(Integer)
    __table_args__ = (CheckConstraint("distance_m > 0", name="ck_distance_positive"),
                      CheckConstraint("from_zone <> to_zone", name="ck_distance_distinct"))
