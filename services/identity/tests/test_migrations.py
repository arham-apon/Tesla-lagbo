"""3.3 / 3.6.2: the 0001_init migration creates the tables and matches models.py exactly."""
import sqlite3

from alembic import command

from conftest import alembic_config


def tables(path) -> set[str]:
    with sqlite3.connect(path) as conn:
        return {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def test_upgrade_creates_all_tables(db_path):
    assert tables(db_path) == {"users", "drivers", "vehicles", "outbox", "alembic_version"}


def test_models_and_migration_agree(db_path):
    # Raises if autogenerate would produce any new operation, i.e. models.py drifted from the migrations.
    command.check(alembic_config(db_path))


def test_downgrade_and_upgrade_again(db_path):
    cfg = alembic_config(db_path)
    command.downgrade(cfg, "base")
    assert tables(db_path) == {"alembic_version"}
    command.upgrade(cfg, "head")
    assert "vehicles" in tables(db_path)
