"""6.4 / 6.7.1: 0001_init creates every table and rule; 0002_seed_tariff adds tariff v1."""
import sqlite3

from alembic import command

from conftest import alembic_config

TABLES = {"tariffs", "quotes", "fares", "wallets", "wallet_transactions", "outbox", "processed_events",
          "alembic_version"}
RULES = {
    "tariffs": ["ck_tariff_nonneg", "ck_tariff_pct"],
    "quotes": ["ck_quote_seats", "ck_quote_zones", "ck_quote_distance", "ck_quote_totals"],
    "fares": ["ck_fare_arithmetic", "ck_fare_nonneg", "ck_fare_method", "ck_fare_status", "ck_fare_parts_nonneg",
              "ck_fare_seats", "ck_fare_discount_only_pooled"],
    "wallets": ["ck_wallet_nonneg"],
    "wallet_transactions": ["ck_wtx_kind", "ck_wtx_nonzero", "ck_wtx_sign", "ck_wtx_ride", "uq_wtx_ride_kind_user"],
}


def query(path, sql, *args):
    with sqlite3.connect(path) as conn:
        return conn.execute(sql, args).fetchall()


def test_tables(db_path):
    assert {r[0] for r in query(db_path, "SELECT name FROM sqlite_master WHERE type='table'")} == TABLES


def test_every_rule_is_in_the_database(db_path):
    for table, names in RULES.items():
        sql = query(db_path, "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", table)[0][0]
        assert all(f"CONSTRAINT {n}" in sql for n in names), table


def test_one_active_tariff_index_keeps_its_where(db_path):
    (sql,) = query(db_path, "SELECT sql FROM sqlite_master WHERE name='uq_tariff_one_active'")[0]
    assert sql.startswith("CREATE UNIQUE INDEX") and "WHERE active = 1" in sql


def test_tariff_v1_is_seeded(db_path):
    assert query(db_path, "SELECT id, base_poysha, per_km_poysha, pool_discount_pct, active FROM tariffs") == \
           [(1, 3000, 1500, 20, 1)]


def test_models_and_migrations_agree(db_path):
    command.check(alembic_config(db_path))


def test_downgrade_steps(db_path):
    cfg = alembic_config(db_path)
    command.downgrade(cfg, "0001")  # tariff removed, tables kept
    assert query(db_path, "SELECT count(*) FROM tariffs") == [(0,)]
    command.downgrade(cfg, "base")
    assert {r[0] for r in query(db_path, "SELECT name FROM sqlite_master WHERE type='table'")} == {"alembic_version"}
    command.upgrade(cfg, "head")
    assert query(db_path, "SELECT count(*) FROM tariffs") == [(1,)]
