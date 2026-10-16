"""The broker's SQLite database: the SQLAlchemy and migration set-up, and bringing the schema up to date."""

import fcntl
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, override

from alembic import command
from flask import current_app
from flask_migrate import Migrate
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import DateTime, event
from sqlalchemy.orm import DeclarativeBase
from sqlalchemy.types import TypeDecorator

if TYPE_CHECKING:
    import sqlite3

    from flask import Flask
    from sqlalchemy.engine import Dialect
    from sqlalchemy.pool import ConnectionPoolEntry


class Base(DeclarativeBase):
    """The declarative base of every model in models.py."""


class UTCDateTime(TypeDecorator):
    """A timezone-aware UTC datetime column.

    SQLite keeps no time zone, so a value is stored as naive UTC and comes back with tzinfo=UTC; a naive
    datetime is refused rather than guessed at.
    """

    impl = DateTime
    cache_ok = True

    @override
    def process_bind_param(self, value: datetime | None, dialect: Dialect) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            msg = f"naive datetime {value!r}: pass a timezone-aware one, e.g. datetime.now(UTC)"
            raise ValueError(msg)
        return value.astimezone(UTC).replace(tzinfo=None)

    @override
    def process_result_value(self, value: datetime | None, dialect: Dialect) -> datetime | None:
        return value.replace(tzinfo=UTC) if value is not None else None


db = SQLAlchemy(model_class=Base)
migrate = Migrate(directory=str(Path(__file__).resolve().parent / "migrations"), render_as_batch=True)


def _db_path() -> Path:
    return Path(os.environ.get("BROKER_DB_PATH", Path(__file__).resolve().parent.parent / "data" / "broker.db"))


def _enable_foreign_keys(dbapi_conn: sqlite3.Connection, _record: ConnectionPoolEntry) -> None:
    dbapi_conn.execute("PRAGMA foreign_keys = ON")


def upgrade_db() -> None:
    """Bring the DB to the latest revision; needs an app context.

    Same as `flask db upgrade`, except it leaves logging alone: env.py's logging setup
    would silence the app's own logger.
    """
    with Path(f"{_db_path()}.migrate.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        config = current_app.extensions["migrate"].migrate.get_config()
        config.attributes["app_startup"] = True
        command.upgrade(config, "head")


def init_app(app: Flask) -> None:
    """Point `app` at the broker's database and bring it to the latest revision."""
    _db_path().parent.mkdir(parents=True, exist_ok=True)
    app.config["SQLALCHEMY_DATABASE_URI"] = f"sqlite:///{_db_path()}"
    db.init_app(app)
    migrate.init_app(app, db)

    from . import models  # noqa: F401, PLC0415 -- registers the models on db.metadata (models imports db)

    with app.app_context():
        event.listen(db.engine, "connect", _enable_foreign_keys)
        upgrade_db()
