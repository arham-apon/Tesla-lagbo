"""4.3: the database refuses nonsense distances and unknown zones."""
import pytest
from sqlalchemy.exc import IntegrityError

from app.models import Zone, ZoneDistance


async def insert_fails(db, row) -> None:
    with pytest.raises(IntegrityError):
        async with db.rw.begin() as s:
            s.add(row)


async def test_distance_must_be_positive(db):
    await insert_fails(db, ZoneDistance(from_zone="UTTARA", to_zone="MIRPUR", distance_m=0))


async def test_distance_needs_two_different_zones(db):
    await insert_fails(db, ZoneDistance(from_zone="UTTARA", to_zone="UTTARA", distance_m=500))


async def test_distance_needs_known_zones(db):
    # Only fails because tesla_common turns on PRAGMA foreign_keys.
    await insert_fails(db, ZoneDistance(from_zone="UTTARA", to_zone="MOTIJHEEL", distance_m=9000))


async def test_one_override_per_direction(db):
    await insert_fails(db, ZoneDistance(from_zone="BANANI", to_zone="MOHAKHALI", distance_m=4000))


async def test_zone_codes_unique(db):
    await insert_fails(db, Zone(code="BANANI", name="Banani again", lat=23.79, lng=90.40))


async def test_valid_override_accepted(db):
    async with db.rw.begin() as s:
        s.add(ZoneDistance(from_zone="UTTARA", to_zone="MIRPUR", distance_m=9800))
