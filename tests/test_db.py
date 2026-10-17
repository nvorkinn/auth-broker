import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from threading import Event, Lock

import pytest
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory
from flask_migrate import downgrade, upgrade
from sqlalchemy import inspect, select

from broker import create_app
from broker import db as db_module
from broker.auth import hash_secret
from broker.db import db
from broker.models import Device, DeviceConfig, Frame, PairingCode, SpotifyToken


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
    """Return each table's CREATE TABLE statement.

    SQLite quotes the name of a table it has renamed, as rebuilding a table does, so the name is
    unquoted to compare like with like.
    """
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


def test_concurrent_app_startup_serializes_migrations(app, monkeypatch):
    with app.app_context():
        downgrade(revision="0005")
        db.engine.dispose()

    first_upgrade_started = Event()
    second_lock_attempted = Event()
    second_upgrade_started = Event()
    release_first_upgrade = Event()
    call_lock = Lock()
    calls = 0
    lock_attempts = 0
    original_upgrade = db_module.command.upgrade
    original_flock = db_module.fcntl.flock

    def pause_first_upgrade(config, revision):
        nonlocal calls
        with call_lock:
            calls += 1
            call_number = calls
        if call_number == 1:
            first_upgrade_started.set()
            assert release_first_upgrade.wait(timeout=5)
        else:
            second_upgrade_started.set()
        return original_upgrade(config, revision)

    def track_flock(file_descriptor, operation):
        nonlocal lock_attempts
        with call_lock:
            lock_attempts += 1
            if lock_attempts == 2:
                second_lock_attempted.set()
        return original_flock(file_descriptor, operation)

    monkeypatch.setattr(db_module.command, "upgrade", pause_first_upgrade)
    monkeypatch.setattr(db_module.fcntl, "flock", track_flock)

    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(create_app)
        assert first_upgrade_started.wait(timeout=5)
        second = executor.submit(create_app)
        try:
            assert second_lock_attempted.wait(timeout=5)
            assert not second_upgrade_started.is_set()
        finally:
            release_first_upgrade.set()
        first_app = first.result(timeout=10)
        second_app = second.result(timeout=10)

    assert calls == 2
    assert _current_revision(first_app) == _head_revision(first_app)
    assert _current_revision(second_app) == _head_revision(second_app)
    for started_app in (first_app, second_app):
        with started_app.app_context():
            db.engine.dispose()


def test_foreign_keys_are_enforced_after_migrating(app):
    with app.app_context():
        db.session.add(
            PairingCode(code="ABC234", device_id="no-such-device", expires_at=datetime(2026, 1, 1, tzinfo=UTC))
        )
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
    """Write a pre-Alembic DB with `scripts`, then start an app on it (as a deploy would)."""
    started = []

    def _adopt(*scripts: str):
        path = tmp_path / "legacy.db"
        conn = sqlite3.connect(path)
        for script in scripts:
            conn.executescript(script)
        conn.commit()
        conn.close()

        monkeypatch.setenv("BROKER_DB_PATH", str(path))
        started.append(create_app())
        return started[-1]

    yield _adopt
    for started_app in started:
        with started_app.app_context():
            db.engine.dispose()


def test_a_legacy_db_keeps_its_data(adopt):
    legacy_app = adopt(
        LEGACY_SCHEMA,
        """
        INSERT INTO devices VALUES ('d', 'hash', '2026-01-01', '2026-01-02', 'kitchen');
        INSERT INTO device_config (device_id, tfl_stop_ids, spotify_enabled) VALUES ('d', '["S1"]', 1);
        INSERT INTO frames VALUES ('d', x'AA55', 'etag', '2026-01-03T00:00:00Z');
        """,
    )

    with legacy_app.app_context():
        device = db.session.get(Device, "d")
        assert (device.created_at, device.paired_at, device.device_name) == (
            datetime(2026, 1, 1, tzinfo=UTC),
            datetime(2026, 1, 2, tzinfo=UTC),
            "kitchen",
        )
        config = db.session.get(DeviceConfig, "d")
        assert (config.tfl_stop_ids, config.spotify_enabled) == (["S1"], True)
        assert config.clock_format == "24h"
        frame = db.session.get(Frame, "d")
        assert (frame.frame, frame.rendered_at) == (b"\xaa\x55", datetime(2026, 1, 3, tzinfo=UTC))


def test_a_legacy_db_ends_up_with_the_same_schema_as_a_fresh_one(app, adopt):
    legacy_app = adopt(LEGACY_SCHEMA)

    assert _current_revision(legacy_app) == _head_revision(legacy_app)
    assert _schema_diff(legacy_app) == []
    assert _ddl(legacy_app) == _ddl(app)


def test_an_early_legacy_db_gets_the_missing_columns_and_tables(app, adopt):
    legacy_app = adopt(OLD_DEVICES_SCHEMA, "INSERT INTO device_config (device_id) VALUES ('d');")

    assert _ddl(legacy_app) == _ddl(app)


def test_adopting_an_early_legacy_db_marks_existing_devices_paired(adopt):
    legacy_app = adopt(
        OLD_DEVICES_SCHEMA,
        """
        INSERT INTO devices VALUES ('paired', 'hash1', '2026-01-01T00:00:00'), ('mid-pairing', 'hash2', '2026-02-01');
        INSERT INTO pairing_codes VALUES ('ABC234', 'mid-pairing', '2026-02-01T00:10:00');
        """,
    )

    with legacy_app.app_context():
        assert db.session.get(Device, "paired").paired_at == datetime(2026, 1, 1, tzinfo=UTC)
        assert db.session.get(Device, "mid-pairing").paired_at is None
        assert db.session.get(Device, "paired").device_name is None


def test_adopting_an_early_legacy_db_defaults_the_postcode(adopt):
    legacy_app = adopt(OLD_DEVICES_SCHEMA, "INSERT INTO devices VALUES ('d', 'hash', '2026-01-01');")

    with legacy_app.app_context():
        db.session.add(DeviceConfig(device_id="d"))
        db.session.commit()
        assert db.session.get(DeviceConfig, "d").postcode == ""


def test_a_legacy_db_with_paired_at_but_no_device_name_gets_device_name(adopt):
    """paired_at and device_name were added in different releases, so a DB can have one and not the other."""
    legacy_app = adopt(
        LEGACY_SCHEMA.replace(",\n                      paired_at TEXT, device_name TEXT", ", paired_at TEXT"),
        "INSERT INTO devices VALUES ('d', 'hash', '2026-01-01', NULL);",
    )

    with legacy_app.app_context():
        device = db.session.get(Device, "d")
        assert (device.paired_at, device.device_name) == (None, None)


def test_starting_twice_leaves_an_adopted_db_alone(adopt, monkeypatch):
    legacy_app = adopt(LEGACY_SCHEMA, "INSERT INTO devices VALUES ('d', 'hash', '2026-01-01', NULL, NULL);")
    again = create_app()

    assert _ddl(again) == _ddl(legacy_app)
    with again.app_context():
        assert db.session.get(Device, "d").paired_at is None
        db.session.remove()
        db.engine.dispose()


# --- 0002: string timestamps to datetimes -------------------------------------------------------


def _at_0001_with(app, statements: str) -> None:
    """Take the DB back to 0001, where timestamps were strings, and insert rows there."""
    with app.app_context():
        downgrade(revision="0001")
        with db.engine.begin() as conn:
            for statement in filter(str.strip, statements.split(";")):
                conn.exec_driver_sql(statement)


def test_0002_converts_every_string_timestamp_shape_to_utc(app):
    _at_0001_with(
        app,
        """
        INSERT INTO devices VALUES
            ('offset', 'h1', '2026-03-01T12:00:00.123456+00:00', '2026-03-01T13:30:00+01:00', NULL);
        INSERT INTO devices VALUES ('zulu', 'h2', '2026-03-02T08:00:00Z', NULL, NULL);
        INSERT INTO devices VALUES ('date-only', 'h3', '2026-01-01', '2026-01-01T09:15:00', NULL);
        INSERT INTO pairing_codes VALUES ('ABC234', 'zulu', '2026-03-02T08:10:00+00:00');
        INSERT INTO frames VALUES ('zulu', x'00', 'etag', '2026-03-02T08:05:00Z');
        INSERT INTO spotify_tokens VALUES ('offset', 'refresh', 'access', '1793865600.5');
        INSERT INTO spotify_tokens VALUES ('zulu', 'refresh', NULL, NULL);
        """,
    )

    with app.app_context():
        upgrade()

        offset = db.session.get(Device, "offset")
        assert offset.created_at == datetime(2026, 3, 1, 12, 0, 0, 123456, tzinfo=UTC)
        assert offset.paired_at == datetime(2026, 3, 1, 12, 30, tzinfo=UTC)  # +01:00 moved to UTC
        zulu = db.session.get(Device, "zulu")
        assert (zulu.created_at, zulu.paired_at) == (datetime(2026, 3, 2, 8, tzinfo=UTC), None)
        date_only = db.session.get(Device, "date-only")
        assert date_only.created_at == datetime(2026, 1, 1, tzinfo=UTC)
        assert date_only.paired_at == datetime(2026, 1, 1, 9, 15, tzinfo=UTC)  # no zone: taken as UTC

        assert db.session.get(PairingCode, "ABC234").expires_at == datetime(2026, 3, 2, 8, 10, tzinfo=UTC)
        assert db.session.get(Frame, "zulu").rendered_at == datetime(2026, 3, 2, 8, 5, tzinfo=UTC)
        assert db.session.get(SpotifyToken, "offset").expires_at == 1793865600.5
        assert db.session.get(SpotifyToken, "zulu").expires_at is None


def test_0002_converted_timestamps_compare_correctly_in_queries(app):
    """Expiry is checked in SQL, so stored values must compare as times, not as their old strings."""
    _at_0001_with(
        app,
        """
        INSERT INTO devices VALUES ('d', 'h', '2026-01-01', NULL, NULL);
        INSERT INTO pairing_codes VALUES ('EARLY2', 'd', '2026-03-02T09:30:00+01:00');
        INSERT INTO pairing_codes VALUES ('LATER3', 'd', '2026-03-02T08:45:00Z');
        """,
    )

    with app.app_context():
        upgrade()
        cutoff = datetime(2026, 3, 2, 8, 40, tzinfo=UTC)
        # As strings, '...09:30:00+01:00' sorts after '...08:45:00Z'; as times it is 08:30 UTC, before the cutoff.
        live = db.session.scalars(select(PairingCode.code).where(PairingCode.expires_at > cutoff)).all()
        assert live == ["LATER3"]


def test_0002_downgrade_gives_back_iso_strings(app):
    with app.app_context():
        db.session.add(Device(device_id="d", renderer_secret_hash="h", created_at=datetime(2026, 3, 1, 12, tzinfo=UTC)))
        db.session.add(SpotifyToken(device_id="d", refresh_token="r", expires_at=1793865600.5))
        db.session.commit()
        db.session.remove()

        downgrade(revision="0001")
        with db.engine.connect() as conn:
            assert conn.exec_driver_sql("SELECT created_at, paired_at FROM devices").one() == (
                "2026-03-01T12:00:00+00:00",
                None,
            )
            assert conn.exec_driver_sql("SELECT expires_at FROM spotify_tokens").scalar() == "1793865600.5"
        upgrade()


def test_0002_fails_on_a_timestamp_it_cannot_read_and_changes_nothing(app):
    _at_0001_with(app, "INSERT INTO devices VALUES ('d', 'h', 'last tuesday', NULL, NULL)")

    with app.app_context():
        with pytest.raises(ValueError, match="last tuesday"):
            upgrade()
        assert _current_revision(app) == "0001"
        with db.engine.connect() as conn:
            assert conn.exec_driver_sql("SELECT created_at FROM devices").scalar() == "last tuesday"


def test_utc_datetime_refuses_a_naive_datetime(app):
    with app.app_context():
        naive = datetime(2026, 1, 1)  # noqa: DTZ001 -- the naive datetime is what is under test
        db.session.add(Device(device_id="d", renderer_secret_hash="h", created_at=naive))
        with pytest.raises(Exception, match="naive datetime"):
            db.session.commit()


# --- 0004: a secret per role ----------------------------------------------------------------------


def test_0004_keeps_existing_devices_and_their_secret_as_the_renderer(app):
    secret = "a-pi-secret-from-before-roles"
    with app.app_context():
        downgrade(revision="0003")
        with db.engine.begin() as conn:
            conn.exec_driver_sql(
                "INSERT INTO devices (device_id, device_secret_hash, created_at, paired_at) VALUES (?, ?, ?, ?)",
                ("old-pi", hash_secret(secret), "2026-03-01 12:00:00", "2026-03-01 12:05:00"),
            )
            conn.exec_driver_sql("INSERT INTO device_config (device_id) VALUES ('old-pi')")
        upgrade()

        device = db.session.get(Device, "old-pi")
        assert device.renderer_secret_hash == hash_secret(secret)
        assert device.display_secret_hash is None
        assert device.paired_at == datetime(2026, 3, 1, 12, 5, tzinfo=UTC)


def test_0004_downgrade_and_upgrade_keep_split_devices(app, split_device):
    device_id, _, _display_secret = split_device
    with app.app_context():
        downgrade(revision="0003")
        upgrade()
        assert db.session.get(Device, device_id).display_secret_hash is None  # the column went away and back


def test_0008_backfills_existing_devices_with_a_480_by_800_screen(app):
    with app.app_context():
        downgrade(revision="0007")
        with db.engine.begin() as conn:
            conn.exec_driver_sql(
                "INSERT INTO devices (device_id, created_at, renderer_secret_hash) VALUES (?, ?, ?)",
                ("old-pi", "2026-03-01 12:00:00", "hash"),
            )
            conn.exec_driver_sql("INSERT INTO device_config (device_id) VALUES ('old-pi')")
        upgrade()

        config = db.session.get(DeviceConfig, "old-pi")
        assert (config.width, config.height) == (480, 800)


def test_0012_backfills_clock_format_and_keeps_existing_settings(app, register_device):
    device_id, _ = register_device()
    with app.app_context():
        config = db.session.get(DeviceConfig, device_id)
        config.weather_location, config.style = "London", "card"
        db.session.commit()
        db.session.remove()

        downgrade(revision="0011")
        assert "clock_format" not in {column["name"] for column in inspect(db.engine).get_columns("device_config")}
        upgrade()

        config = db.session.get(DeviceConfig, device_id)
        assert config.clock_format == "24h"
        assert (config.weather_location, config.style) == ("London", "card")


def test_clock_format_defaults_to_24_hour_for_orm_and_raw_inserts(app):
    with app.app_context():
        for device_id in ("orm", "raw"):
            db.session.add(Device(device_id=device_id, created_at=datetime.now(UTC)))
        db.session.flush()
        db.session.add(DeviceConfig(device_id="orm"))
        db.session.execute(db.text("INSERT INTO device_config (device_id) VALUES ('raw')"))
        db.session.commit()

        for device_id in ("orm", "raw"):
            assert db.session.get(DeviceConfig, device_id).clock_format == "24h"
        column = next(
            column for column in inspect(db.engine).get_columns("device_config") if column["name"] == "clock_format"
        )
        assert column["nullable"] is False


def test_0013_backfills_english_and_keeps_existing_settings(app, register_device):
    device_id, _ = register_device()
    with app.app_context():
        config = db.session.get(DeviceConfig, device_id)
        config.clock_format, config.tfl_stop_ids = "12h", ["940GZZLUKNG", "NSR:Quay:7333"]
        db.session.commit()
        db.session.remove()

        downgrade(revision="0012")
        assert "language" not in {column["name"] for column in inspect(db.engine).get_columns("device_config")}
        upgrade()

        config = db.session.get(DeviceConfig, device_id)
        assert config.language == "en"
        assert (config.clock_format, config.tfl_stop_ids) == ("12h", ["940GZZLUKNG", "NSR:Quay:7333"])


# --- 0015: classic retired --------------------------------------------------------------------------


def _styles(columns):
    """Every device's values of `columns`, read with SQL, as the models no longer have the old style columns."""
    with db.engine.begin() as conn:
        rows = conn.exec_driver_sql(f"SELECT device_id, {', '.join(columns)} FROM device_config").all()  # noqa: S608
    return {row[0]: tuple(row[1:]) for row in rows}


def _insert_device(conn, device_id, **columns):
    conn.exec_driver_sql("INSERT INTO devices (device_id, created_at) VALUES (?, '2026-10-01 12:00:00')", (device_id,))
    names = ", ".join(["device_id", *columns])
    marks = ", ".join("?" * (len(columns) + 1))
    conn.exec_driver_sql(f"INSERT INTO device_config ({names}) VALUES ({marks})", (device_id, *columns.values()))  # noqa: S608


def test_0015_moves_classic_devices_to_hero_and_keeps_the_others(app):
    with app.app_context():
        downgrade(revision="0014")
        with db.engine.begin() as conn:
            _insert_device(conn, "old", tfl_style="classic", spotify_style="classic")
            _insert_device(conn, "picked", tfl_style="card", spotify_style="airy")
        upgrade(revision="0015")

        assert _styles(("tfl_style", "spotify_style")) == {"old": ("hero", "hero"), "picked": ("card", "airy")}


# --- 0016: the notice board style ------------------------------------------------------------------


def test_0016_starts_every_device_on_the_hero_notices_style(app):
    with app.app_context():
        downgrade(revision="0015")
        assert "notices_style" not in {column["name"] for column in inspect(db.engine).get_columns("device_config")}
        with db.engine.begin() as conn:
            _insert_device(conn, "screen", postcode="SW1A 1AA", spotify_style="card")
        upgrade(revision="0016")

        assert _styles(("notices_style", "postcode", "spotify_style")) == {"screen": ("hero", "SW1A 1AA", "card")}


# --- 0017: one style for every panel ----------------------------------------------------------------


def test_0017_gives_each_device_the_style_of_its_departures(app):
    """Grouped is airy's old name; the Spotify and notice board styles give way to the departures'."""
    with app.app_context():
        downgrade(revision="0016")
        with db.engine.begin() as conn:
            _insert_device(conn, "grouped", tfl_style="grouped", spotify_style="card", notices_style="hero")
            _insert_device(conn, "card", tfl_style="card", spotify_style="airy", notices_style="boxed")
            _insert_device(conn, "hero", tfl_style="hero", spotify_style="airy", notices_style="airy")
        upgrade()

        styles = {c.device_id: c.style for c in db.session.scalars(select(DeviceConfig))}
        assert styles == {"grouped": "airy", "card": "card", "hero": "hero"}
        columns = {column["name"] for column in inspect(db.engine).get_columns("device_config")}
        assert not columns & {"tfl_style", "spotify_style", "notices_style"}


def test_0017_downgrade_gives_every_panel_the_one_style(app, register_device):
    airy, _ = register_device()
    with app.app_context():
        db.session.get(DeviceConfig, airy).style = "airy"
        db.session.commit()
        db.session.remove()

        downgrade(revision="0016")

        assert _styles(("tfl_style", "spotify_style", "notices_style")) == {airy: ("grouped", "airy", "airy")}


# --- 0018: airy by default --------------------------------------------------------------------------


def test_0018_starts_new_devices_on_airy_and_keeps_the_others(app):
    with app.app_context():
        downgrade(revision="0017")
        with db.engine.begin() as conn:
            _insert_device(conn, "before")
            _insert_device(conn, "picked", style="card")
        upgrade()
        with db.engine.begin() as conn:
            _insert_device(conn, "after")

        assert _styles(("style",)) == {"before": ("hero",), "picked": ("card",), "after": ("airy",)}


# --- 0021: the picked weather place ---------------------------------------------------------------------------------


def test_0021_keeps_existing_weather_locations_and_leaves_their_place_to_the_backfill(app):
    with app.app_context():
        downgrade(revision="0020")
        assert "weather_place_id" not in {column["name"] for column in inspect(db.engine).get_columns("device_config")}
        with db.engine.begin() as conn:
            _insert_device(conn, "old", weather_location="Tromsø", postcode="9008")
        upgrade(revision="0021")

        config = db.session.get(DeviceConfig, "old")
        assert (config.weather_location, config.postcode) == ("Tromsø", "9008")
        assert (config.weather_place_id, config.weather_place_name, config.weather_lat) == (None, None, None)
