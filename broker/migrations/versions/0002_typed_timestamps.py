"""Store timestamps as datetimes, and Spotify token expiry as a number.

Every timestamp was an ISO-8601 string in whatever shape its writer chose: "+00:00" or "Z", and in
databases from early releases sometimes a bare date or no zone at all. They become DATETIME columns
holding naive UTC, which UTCDateTime in broker/db.py reads back as aware UTC. A string with no zone
is taken to be UTC already, as every writer used UTC. spotify_tokens.expires_at was a Unix
timestamp kept as text and becomes a FLOAT.

Changing a column's type in SQLite rebuilds the table and copies values across unchanged, so each
value is read before the rebuild and written back converted afterwards. A value that won't parse
fails the migration, which rolls back and leaves the database as it was.

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-05 10:00:00.000000

"""

from collections.abc import Sequence
from datetime import UTC, datetime

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0002"
down_revision: str | Sequence[str] | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Alembic looks these up by name, so export them rather than leave them looking unused.
__all__ = ["revision", "down_revision", "branch_labels", "depends_on", "upgrade", "downgrade"]

# table -> (primary key, [(timestamp column, nullable)])
TIMESTAMPS = {
    "devices": ("device_id", [("created_at", False), ("paired_at", True)]),
    "pairing_codes": ("code", [("expires_at", False)]),
    "frames": ("device_id", [("rendered_at", False)]),
}


def _to_naive_utc(value: str | None) -> datetime | None:
    if value is None:
        return None
    parsed = datetime.fromisoformat(value)
    return parsed.astimezone(UTC).replace(tzinfo=None) if parsed.tzinfo else parsed


def _read(bind, table: str, key: str, columns: list[str], type_: sa.types.TypeEngine) -> list[dict]:
    t = sa.table(table, sa.column(key, sa.String()), *(sa.column(c, type_) for c in columns))
    return [dict(row._mapping) for row in bind.execute(sa.select(t))]


def _write(bind, table: str, key: str, rows: list[dict], type_: sa.types.TypeEngine) -> None:
    if not rows:
        return
    columns = [c for c in rows[0] if c != key]
    t = sa.table(table, sa.column(key, sa.String()), *(sa.column(c, type_) for c in columns))
    for row in rows:
        bind.execute(t.update().where(t.c[key] == row[key]).values({c: row[c] for c in columns}))


def _alter(table: str, columns: list[tuple[str, bool]], from_type, to_type) -> None:
    with op.batch_alter_table(table) as batch:
        for column, nullable in columns:
            batch.alter_column(column, existing_type=from_type, type_=to_type, existing_nullable=nullable)


def upgrade() -> None:
    bind = op.get_bind()

    for table, (key, columns) in TIMESTAMPS.items():
        names = [c for c, _ in columns]
        rows = _read(bind, table, key, names, sa.String())
        converted = [{key: row[key], **{c: _to_naive_utc(row[c]) for c in names}} for row in rows]
        _alter(table, columns, sa.String(), sa.DateTime())
        _write(bind, table, key, converted, sa.DateTime())

    tokens = _read(bind, "spotify_tokens", "device_id", ["expires_at"], sa.String())
    converted = [
        {"device_id": t["device_id"], "expires_at": None if t["expires_at"] is None else float(t["expires_at"])}
        for t in tokens
    ]
    _alter("spotify_tokens", [("expires_at", True)], sa.String(), sa.Float())
    _write(bind, "spotify_tokens", "device_id", converted, sa.Float())


def downgrade() -> None:
    bind = op.get_bind()

    for table, (key, columns) in TIMESTAMPS.items():
        names = [c for c, _ in columns]
        rows = _read(bind, table, key, names, sa.DateTime())
        converted = [
            {key: row[key], **{c: row[c] and row[c].replace(tzinfo=UTC).isoformat() for c in names}} for row in rows
        ]
        _alter(table, columns, sa.DateTime(), sa.String())
        _write(bind, table, key, converted, sa.String())

    tokens = _read(bind, "spotify_tokens", "device_id", ["expires_at"], sa.Float())
    converted = [
        {"device_id": t["device_id"], "expires_at": None if t["expires_at"] is None else str(t["expires_at"])}
        for t in tokens
    ]
    _alter("spotify_tokens", [("expires_at", True)], sa.Float(), sa.String())
    _write(bind, "spotify_tokens", "device_id", converted, sa.String())
