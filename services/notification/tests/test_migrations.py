"""7.3: 0001_init creates the inbox and processed_events, with the index catch-up needs and the CHECK rules."""
import sqlite3

from alembic import command

from conftest import alembic_config


def query(path, sql, *args):
    with sqlite3.connect(path) as conn:
        return conn.execute(sql, args).fetchall()


def test_tables(db_path):
    assert {r[0] for r in query(db_path, "SELECT name FROM sqlite_master WHERE type='table'")} == \
           {"notifications", "processed_events", "alembic_version"}


def test_rules_are_in_the_database(db_path):
    (sql,) = query(db_path, "SELECT sql FROM sqlite_master WHERE name='notifications'")[0]
    assert "CONSTRAINT ck_notif_payload_json" in sql and "CONSTRAINT ck_notif_user" in sql


def test_catch_up_query_uses_the_index(db_path):
    """GET /notifications?after_id= (7.5) must not read everyone's inbox to find Nusrat's new rows."""
    plan = query(db_path, "EXPLAIN QUERY PLAN SELECT * FROM notifications WHERE user_id = ? AND id > ? "
                          "ORDER BY id LIMIT 50", "u", 0)
    assert any("ix_notif_user_id" in row[-1] for row in plan), plan
    assert not any("TEMP B-TREE" in row[-1] for row in plan)  # already in order: no sorting step


def test_models_and_migrations_agree(db_path):
    command.check(alembic_config(db_path))


def test_downgrade_and_upgrade_again(db_path):
    cfg = alembic_config(db_path)
    command.downgrade(cfg, "base")
    assert {r[0] for r in query(db_path, "SELECT name FROM sqlite_master WHERE type='table'")} == {"alembic_version"}
    command.upgrade(cfg, "head")
    assert query(db_path, "SELECT count(*) FROM notifications") == [(0,)]
