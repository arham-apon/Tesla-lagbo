"""seed tariff

Revision ID: 0002
Revises: 0001
Create Date: 2026-09-24 16:00:00

Tariff v1 (plan 6.2 / 6.7 step 1): base 3000 poysha (30 taka), 1500 poysha per km, 20 % pool discount.
Written out here, not imported from app/, so this migration keeps doing the same thing if the app changes.
A future price change is a NEW migration adding tariff 2 (active) and setting tariff 1 inactive; tariff 1 must
stay, because old quotes point at it.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '0002'
down_revision: Union[str, Sequence[str], None] = '0001'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

tariffs = sa.table("tariffs", sa.column("id", sa.Integer), sa.column("base_poysha", sa.Integer),
                   sa.column("per_km_poysha", sa.Integer), sa.column("pool_discount_pct", sa.Integer),
                   sa.column("active", sa.Boolean))


def upgrade() -> None:
    op.bulk_insert(tariffs, [{"id": 1, "base_poysha": 3000, "per_km_poysha": 1500, "pool_discount_pct": 20,
                              "active": True}])


def downgrade() -> None:
    op.execute(tariffs.delete().where(tariffs.c.id == 1))
