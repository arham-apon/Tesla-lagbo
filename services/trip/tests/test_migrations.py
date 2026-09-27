"""5.3 / 5.9.1: 0001_init creates every table, and the rules autogenerate is known to lose really are in the database."""
import sqlite3

from alembic import command

from conftest import alembic_config

TABLES = {"driver_shifts", "pools", "ride_requests", "pool_waypoints", "ride_status_history", "ride_offers",
          "outbox", "processed_events", "alembic_version"}


def query(path, sql, *args):
    with sqlite3.connect(path) as conn:
        return conn.execute(sql, args).fetchall()


def table_sql(path, name) -> str:
    return query(path, "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", name)[0][0]


def test_tables(db_path):
    assert {r[0] for r in query(db_path, "SELECT name FROM sqlite_master WHERE type='table'")} == TABLES


def test_partial_unique_indexes_keep_their_where(db_path):
    # Plan 5.9 step 1: autogenerate can drop sqlite_where, which would silently make these FULL unique indexes
    # (one pool per driver EVER, one ride per passenger EVER).
    sql = dict(query(db_path, "SELECT name, sql FROM sqlite_master WHERE type='index' AND name LIKE 'uq_%'"))
    assert "WHERE status IN ('FORMING','IN_PROGRESS')" in sql["uq_pool_one_live_per_driver"]
    assert "WHERE status IN ('REQUESTED','MATCHED','DRIVER_ARRIVED','STARTED')" in \
           sql["uq_ride_one_active_per_passenger"]
    assert all(s.startswith("CREATE UNIQUE INDEX") for s in sql.values())


def test_every_check_rule_is_in_the_database(db_path):
    expected = {
        "driver_shifts": ["ck_shift_capacity"],
        "pools": ["ck_pool_max", "ck_pool_capacity", "ck_pool_status"],
        "ride_requests": ["ck_ride_seats", "ck_ride_zones", "ck_ride_status", "ck_ride_payment", "ck_ride_fare",
                          "ck_ride_pool_when_matched"],
        "pool_waypoints": ["ck_waypoint_seq", "ck_waypoint_kind", "uq_waypoint_seq"],
        "ride_status_history": ["ck_history_actor"],
        "ride_offers": ["ck_offer_status"],
    }
    for table, names in expected.items():
        sql = table_sql(db_path, table)
        assert all(f"CONSTRAINT {n}" in sql for n in names), table


def test_models_and_migrations_agree(db_path):
    command.check(alembic_config(db_path))


def test_downgrade_and_upgrade_again(db_path):
    cfg = alembic_config(db_path)
    command.downgrade(cfg, "base")
    assert {r[0] for r in query(db_path, "SELECT name FROM sqlite_master WHERE type='table'")} == {"alembic_version"}
    command.upgrade(cfg, "head")
    assert {r[0] for r in query(db_path, "SELECT name FROM sqlite_master WHERE type='table'")} == TABLES
