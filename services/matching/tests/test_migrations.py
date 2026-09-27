"""4.3 / 4.8.1: 0001_init creates the tables, 0002_seed_zones loads the 9 zones and the 3 overrides both ways."""
import sqlite3

from alembic import command

from conftest import alembic_config

PLAN_ZONES = {
    "BANANI": ("Banani", 23.7937, 90.4066),
    "GULSHAN_1": ("Gulshan 1", 23.7806, 90.4163),
    "GULSHAN_2": ("Gulshan 2", 23.7925, 90.4144),
    "MOHAKHALI": ("Mohakhali", 23.7780, 90.4050),
    "FARMGATE": ("Farmgate", 23.7580, 90.3897),
    "DHANMONDI": ("Dhanmondi", 23.7465, 90.3760),
    "MIRPUR": ("Mirpur", 23.8069, 90.3687),
    "UTTARA": ("Uttara", 23.8759, 90.3795),
    "BASHUNDHARA": ("Bashundhara", 23.8193, 90.4526),
}


def query(path, sql):
    with sqlite3.connect(path) as conn:
        return conn.execute(sql).fetchall()


def test_tables(db_path):
    names = {r[0] for r in query(db_path, "SELECT name FROM sqlite_master WHERE type='table'")}
    assert names == {"zones", "zone_distances", "alembic_version"}


def test_nine_zones_exactly_as_in_the_plan(db_path):
    rows = query(db_path, "SELECT code, name, lat, lng FROM zones")
    assert {code: (name, lat, lng) for code, name, lat, lng in rows} == PLAN_ZONES


def test_overrides_in_both_directions(db_path):
    rows = set(query(db_path, "SELECT from_zone, to_zone, distance_m FROM zone_distances"))
    assert rows == {("BANANI", "MOHAKHALI", 3500), ("MOHAKHALI", "BANANI", 3500),
                    ("BANANI", "GULSHAN_1", 2000), ("GULSHAN_1", "BANANI", 2000),
                    ("GULSHAN_1", "MOHAKHALI", 2000), ("MOHAKHALI", "GULSHAN_1", 2000)}


def test_models_and_migrations_agree(db_path):
    command.check(alembic_config(db_path))


def test_downgrade_steps(db_path):
    cfg = alembic_config(db_path)
    command.downgrade(cfg, "0001")  # seed removed, tables kept
    assert query(db_path, "SELECT count(*) FROM zones") == [(0,)]
    command.downgrade(cfg, "base")
    assert {r[0] for r in query(db_path, "SELECT name FROM sqlite_master WHERE type='table'")} == {"alembic_version"}
    command.upgrade(cfg, "head")
    assert query(db_path, "SELECT count(*) FROM zones") == [(9,)]
