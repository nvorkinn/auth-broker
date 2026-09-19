import os
import sqlite3
from pathlib import Path

from flask import g

SCHEMA = """
CREATE TABLE IF NOT EXISTS devices (
    device_id TEXT PRIMARY KEY,
    device_secret_hash TEXT NOT NULL,
    created_at TEXT NOT NULL,
    paired_at TEXT
);

CREATE TABLE IF NOT EXISTS pairing_codes (
    code TEXT PRIMARY KEY,
    device_id TEXT NOT NULL REFERENCES devices(device_id),
    expires_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS device_config (
    device_id TEXT PRIMARY KEY REFERENCES devices(device_id),
    interval INTEGER NOT NULL DEFAULT 15,
    weather_location TEXT NOT NULL DEFAULT '',
    spotify_enabled INTEGER NOT NULL DEFAULT 0,
    tfl_stop_ids TEXT NOT NULL DEFAULT '[]'
);

CREATE TABLE IF NOT EXISTS spotify_tokens (
    device_id TEXT PRIMARY KEY REFERENCES devices(device_id),
    refresh_token TEXT NOT NULL,
    access_token TEXT,
    expires_at TEXT
);

CREATE TABLE IF NOT EXISTS glowmarkt_credentials (
    device_id TEXT PRIMARY KEY REFERENCES devices(device_id),
    username TEXT NOT NULL DEFAULT '',
    password_encrypted TEXT
);
"""


def _db_path() -> Path:
    return Path(os.environ.get("BROKER_DB_PATH", Path(__file__).resolve().parent.parent / "data" / "broker.db"))


def get_db() -> sqlite3.Connection:
    if "db" not in g:
        g.db = sqlite3.connect(_db_path())
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys = ON")
    return g.db


def close_db(_exc=None) -> None:
    db = g.pop("db", None)
    if db is not None:
        db.close()


def _migrate(conn: sqlite3.Connection) -> None:
    columns = {row[1] for row in conn.execute("PRAGMA table_info(devices)")}
    if "paired_at" in columns:
        return
    conn.execute("ALTER TABLE devices ADD COLUMN paired_at TEXT")
    # A device with an unredeemed code is mid-pairing; any other existing device is treated as paired.
    conn.execute(
        "UPDATE devices SET paired_at = created_at WHERE device_id NOT IN (SELECT device_id FROM pairing_codes)"
    )


def init_app(app) -> None:
    path = _db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.executescript(SCHEMA)
    _migrate(conn)
    conn.commit()
    conn.close()

    app.teardown_appcontext(close_db)
