"""seed zones

Revision ID: 0002
Revises: 0001
Create Date: 2026-09-24 01:19:40.062513

The 9 Dhaka zones and the hand-set distance overrides (plan 4.3). Data is written out here, not imported
from app/, so this migration keeps doing the same thing even if the app code changes later.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '0002'
down_revision: Union[str, Sequence[str], None] = '0001'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

zones = sa.table("zones", sa.column("code", sa.String), sa.column("name", sa.String),
                 sa.column("lat", sa.Float), sa.column("lng", sa.Float))
zone_distances = sa.table("zone_distances", sa.column("from_zone", sa.String),
                          sa.column("to_zone", sa.String), sa.column("distance_m", sa.Integer))

ZONES = [
    ("BANANI", "Banani", 23.7937, 90.4066),
    ("GULSHAN_1", "Gulshan 1", 23.7806, 90.4163),
    ("GULSHAN_2", "Gulshan 2", 23.7925, 90.4144),
    ("MOHAKHALI", "Mohakhali", 23.7780, 90.4050),
    ("FARMGATE", "Farmgate", 23.7580, 90.3897),
    ("DHANMONDI", "Dhanmondi", 23.7465, 90.3760),
    ("MIRPUR", "Mirpur", 23.8069, 90.3687),
    ("UTTARA", "Uttara", 23.8759, 90.3795),
    ("BASHUNDHARA", "Bashundhara", 23.8193, 90.4526),
]
# Inserted in both directions so A->B and B->A always agree.
OVERRIDES = [
    ("BANANI", "MOHAKHALI", 3500),
    ("BANANI", "GULSHAN_1", 2000),
    ("GULSHAN_1", "MOHAKHALI", 2000),
]


def upgrade() -> None:
    op.bulk_insert(zones, [{"code": c, "name": n, "lat": lat, "lng": lng} for c, n, lat, lng in ZONES])
    op.bulk_insert(zone_distances, [{"from_zone": x, "to_zone": y, "distance_m": d}
                                    for a, b, d in OVERRIDES for x, y in ((a, b), (b, a))])


def downgrade() -> None:
    op.execute(zone_distances.delete())
    op.execute(zones.delete())
