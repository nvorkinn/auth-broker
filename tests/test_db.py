import sqlite3

from broker.db import _migrate, get_db


def test_schema_creates_expected_tables(app):
    with app.app_context():
        tables = {
            row["name"] for row in get_db().execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        }
    assert {"devices", "pairing_codes", "device_config", "spotify_tokens"} <= tables


def test_get_db_reuses_connection_within_app_context(app):
    with app.app_context():
        assert get_db() is get_db()


OLD_DEVICES_SCHEMA = """
CREATE TABLE devices (device_id TEXT PRIMARY KEY, device_secret_hash TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE pairing_codes (code TEXT PRIMARY KEY, device_id TEXT NOT NULL, expires_at TEXT NOT NULL);
"""


def test_migration_adds_paired_at_and_marks_existing_devices(tmp_path):
    conn = sqlite3.connect(tmp_path / "old.db")
    conn.executescript(OLD_DEVICES_SCHEMA)
    conn.executemany(
        "INSERT INTO devices VALUES (?, 'hash', ?)", [("paired", "2026-01-01T00:00:00"), ("mid-pairing", "2026-02-01")]
    )
    conn.execute("INSERT INTO pairing_codes VALUES ('ABC234', 'mid-pairing', '2026-02-01T00:10:00')")

    _migrate(conn)

    rows = dict(conn.execute("SELECT device_id, paired_at FROM devices"))
    assert rows == {"paired": "2026-01-01T00:00:00", "mid-pairing": None}


def test_migration_is_a_no_op_the_second_time(tmp_path):
    conn = sqlite3.connect(tmp_path / "old.db")
    conn.executescript(OLD_DEVICES_SCHEMA)
    conn.execute("INSERT INTO devices VALUES ('d', 'hash', '2026-01-01')")
    _migrate(conn)
    conn.execute("UPDATE devices SET paired_at = NULL")

    _migrate(conn)

    assert conn.execute("SELECT paired_at FROM devices").fetchone()[0] is None


def test_migration_adds_device_name_column(tmp_path):
    conn = sqlite3.connect(tmp_path / "old.db")
    conn.executescript(OLD_DEVICES_SCHEMA)
    conn.execute("INSERT INTO devices VALUES ('d', 'hash', '2026-01-01')")

    _migrate(conn)

    columns = {row[1] for row in conn.execute("PRAGMA table_info(devices)")}
    assert "device_name" in columns
    assert conn.execute("SELECT device_name FROM devices WHERE device_id = 'd'").fetchone()[0] is None


def test_migration_adds_device_name_to_a_db_that_already_has_paired_at(tmp_path):
    """A DB that already ran the paired_at migration in the past must still pick up
    device_name later -- the two migrations must not gate on the same early return."""
    conn = sqlite3.connect(tmp_path / "old.db")
    conn.executescript(OLD_DEVICES_SCHEMA)
    conn.execute("INSERT INTO devices VALUES ('d', 'hash', '2026-01-01')")
    _migrate(conn)

    conn.execute("ALTER TABLE devices DROP COLUMN device_name")
    _migrate(conn)

    columns = {row[1] for row in conn.execute("PRAGMA table_info(devices)")}
    assert "device_name" in columns
