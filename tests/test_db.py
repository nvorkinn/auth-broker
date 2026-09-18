from broker.db import get_db


def test_schema_creates_expected_tables(app):
    with app.app_context():
        tables = {
            row["name"] for row in get_db().execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        }
    assert {"devices", "pairing_codes", "device_config", "spotify_tokens"} <= tables


def test_get_db_reuses_connection_within_app_context(app):
    with app.app_context():
        assert get_db() is get_db()
