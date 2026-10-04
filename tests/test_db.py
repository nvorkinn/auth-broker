import sqlite3

import pytest
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory
from flask_migrate import downgrade, upgrade
from sqlalchemy import inspect

from broker.db import db
from broker.models import Device, DeviceConfig, Frame, PairingCode


def _current_revision(app):
    with app.app_context(), db.engine.connect() as conn:
        return MigrationContext.configure(conn).get_current_revision()


def _head_revision(app):
    with app.app_context():
        return ScriptDirectory.from_config(app.extensions["migrate"].migrate.get_config()).get_current_head()


def _schema_diff(app):
    with app.app_context(), db.engine.connect() as conn:
        return compare_metadata(MigrationContext.configure(conn), db.metadata)


def _ddl(app):
    """Each table's CREATE TABLE statement. SQLite quotes the name of a table it has renamed, as
    rebuilding a table does, so the name is unquoted to compare like with like."""
    with app.app_context(), db.engine.connect() as conn:
        rows = conn.exec_driver_sql("SELECT name, sql FROM sqlite_master WHERE type = 'table'").all()
    return {name: sql.replace(f'CREATE TABLE "{name}"', f"CREATE TABLE {name}") for name, sql in rows}


def test_schema_creates_expected_tables(app):
    with app.app_context():
        tables = set(inspect(db.engine).get_table_names())
    assert {"devices", "pairing_codes", "device_config", "spotify_tokens", "glowmarkt_credentials", "frames"} <= tables


def test_a_fresh_db_is_migrated_to_head(app):
    assert _current_revision(app) == _head_revision(app)


def test_migrations_match_the_models(app):
    """Fails when a model changes without a migration: run `flask --app wsgi db migrate -m "..."`."""
    assert _schema_diff(app) == []


def test_migrations_downgrade_and_upgrade_again(app):
    with app.app_context():
        downgrade(revision="base")
        assert set(inspect(db.engine).get_table_names()) == {"alembic_version"}
        upgrade()
    assert _current_revision(app) == _head_revision(app)


def test_foreign_keys_are_enforced_after_migrating(app):
    with app.app_context():
        db.session.add(PairingCode(code="ABC234", device_id="no-such-device", expires_at="2026-01-01"))
        with pytest.raises(Exception, match="FOREIGN KEY"):
            db.session.commit()


def test_session_is_reused_within_app_context(app):
    with app.app_context():
        assert db.session() is db.session()


# --- Adopting a DB from before Alembic ------------------------------------------------------------

# The schema as the last pre-Alembic release left it.
LEGACY_SCHEMA = """
CREATE TABLE devices (device_id TEXT PRIMARY KEY, device_secret_hash TEXT NOT NULL, created_at TEXT NOT NULL,
                      paired_at TEXT, device_name TEXT);
CREATE TABLE pairing_codes (code TEXT PRIMARY KEY, device_id TEXT NOT NULL REFERENCES devices(device_id),
                            expires_at TEXT NOT NULL);
CREATE TABLE device_config (device_id TEXT PRIMARY KEY REFERENCES devices(device_id),
                            interval INTEGER NOT NULL DEFAULT 15, weather_location TEXT NOT NULL DEFAULT '',
                            postcode TEXT NOT NULL DEFAULT '', spotify_enabled INTEGER NOT NULL DEFAULT 0,
                            tfl_stop_ids TEXT NOT NULL DEFAULT '[]');
CREATE TABLE spotify_tokens (device_id TEXT PRIMARY KEY REFERENCES devices(device_id), refresh_token TEXT NOT NULL,
                             access_token TEXT, expires_at TEXT);
CREATE TABLE glowmarkt_credentials (device_id TEXT PRIMARY KEY REFERENCES devices(device_id),
                                    username TEXT NOT NULL DEFAULT '', password_encrypted TEXT);
CREATE TABLE frames (device_id TEXT PRIMARY KEY REFERENCES devices(device_id), frame BLOB NOT NULL,
                     etag TEXT NOT NULL, rendered_at TEXT NOT NULL);
"""

# An early release's schema: before paired_at, device_name, device_config.postcode and several tables.
OLD_DEVICES_SCHEMA = """
CREATE TABLE devices (device_id TEXT PRIMARY KEY, device_secret_hash TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE pairing_codes (code TEXT PRIMARY KEY, device_id TEXT NOT NULL, expires_at TEXT NOT NULL);
CREATE TABLE device_config (device_id TEXT PRIMARY KEY REFERENCES devices(device_id),
                            interval INTEGER NOT NULL DEFAULT 15, weather_location TEXT NOT NULL DEFAULT '',
                            spotify_enabled INTEGER NOT NULL DEFAULT 0, tfl_stop_ids TEXT NOT NULL DEFAULT '[]');
"""


@pytest.fixture
def adopt(app, tmp_path, monkeypatch):
    """Writes a pre-Alembic DB with `script`, then starts an app on it (as a deploy would)."""
    started = []

    def _adopt(script: str):
        path = tmp_path / "legacy.db"
        conn = sqlite3.connect(path)
        conn.executescript(script)
        conn.commit()
        conn.close()

        monkeypatch.setenv("BROKER_DB_PATH", str(path))
        from broker import create_app

        started.append(create_app())
        return started[-1]

    yield _adopt
    for started_app in started:
        with started_app.app_context():
            db.engine.dispose()


def test_a_legacy_db_keeps_its_data(adopt):
    legacy_app = adopt(
        LEGACY_SCHEMA
        + """
        INSERT INTO devices VALUES ('d', 'hash', '2026-01-01', '2026-01-02', 'kitchen');
        INSERT INTO device_config (device_id, tfl_stop_ids, spotify_enabled) VALUES ('d', '["S1"]', 1);
        INSERT INTO frames VALUES ('d', x'AA55', 'etag', '2026-01-03T00:00:00Z');
        """
    )

    with legacy_app.app_context():
        device = db.session.get(Device, "d")
        assert (device.created_at, device.paired_at, device.device_name) == ("2026-01-01", "2026-01-02", "kitchen")
        config = db.session.get(DeviceConfig, "d")
        assert (config.tfl_stop_ids, config.spotify_enabled, config.interval) == (["S1"], True, 15)
        assert db.session.get(Frame, "d").frame == b"\xaa\x55"


def test_a_legacy_db_ends_up_with_the_same_schema_as_a_fresh_one(app, adopt):
    legacy_app = adopt(LEGACY_SCHEMA)

    assert _current_revision(legacy_app) == _head_revision(legacy_app)
    assert _schema_diff(legacy_app) == []
    assert _ddl(legacy_app) == _ddl(app)


def test_an_early_legacy_db_gets_the_missing_columns_and_tables(app, adopt):
    legacy_app = adopt(OLD_DEVICES_SCHEMA + "INSERT INTO device_config (device_id) VALUES ('d');")

    assert _ddl(legacy_app) == _ddl(app)


def test_adopting_an_early_legacy_db_marks_existing_devices_paired(adopt):
    legacy_app = adopt(
        OLD_DEVICES_SCHEMA
        + """
        INSERT INTO devices VALUES ('paired', 'hash', '2026-01-01T00:00:00'), ('mid-pairing', 'hash', '2026-02-01');
        INSERT INTO pairing_codes VALUES ('ABC234', 'mid-pairing', '2026-02-01T00:10:00');
        """
    )

    with legacy_app.app_context():
        assert db.session.get(Device, "paired").paired_at == "2026-01-01T00:00:00"
        assert db.session.get(Device, "mid-pairing").paired_at is None
        assert db.session.get(Device, "paired").device_name is None


def test_adopting_an_early_legacy_db_defaults_the_postcode(adopt):
    legacy_app = adopt(OLD_DEVICES_SCHEMA + "INSERT INTO devices VALUES ('d', 'hash', '2026-01-01');")

    with legacy_app.app_context():
        db.session.add(DeviceConfig(device_id="d"))
        db.session.commit()
        assert db.session.get(DeviceConfig, "d").postcode == ""


def test_a_legacy_db_with_paired_at_but_no_device_name_gets_device_name(adopt):
    """paired_at and device_name were added in different releases, so a DB can have one and not the other."""
    legacy_app = adopt(
        LEGACY_SCHEMA.replace(",\n                      paired_at TEXT, device_name TEXT", ", paired_at TEXT")
        + "INSERT INTO devices VALUES ('d', 'hash', '2026-01-01', NULL);"
    )

    with legacy_app.app_context():
        device = db.session.get(Device, "d")
        assert (device.paired_at, device.device_name) == (None, None)


def test_starting_twice_leaves_an_adopted_db_alone(adopt, monkeypatch):
    legacy_app = adopt(LEGACY_SCHEMA + "INSERT INTO devices VALUES ('d', 'hash', '2026-01-01', NULL, NULL);")
    from broker import create_app

    again = create_app()

    assert _ddl(again) == _ddl(legacy_app)
    with again.app_context():
        assert db.session.get(Device, "d").paired_at is None
        db.session.remove()
        db.engine.dispose()
