"""Baseline: the schema as it stood when the broker moved to Alembic.

A DB from before Alembic (tables, but no alembic_version) is adopted here rather than created:
its old column additions are applied, then every table is rebuilt from the definitions below,
keeping its rows, so it ends up with exactly the DDL a fresh DB gets.

Revision ID: 0001
Revises:
Create Date: 2026-10-04 21:13:21.855676

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0001"
down_revision: str | Sequence[str] | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

metadata = sa.MetaData()

devices = sa.Table(
    "devices",
    metadata,
    sa.Column("device_id", sa.String(), primary_key=True),
    sa.Column("device_secret_hash", sa.String(), nullable=False),
    sa.Column("created_at", sa.String(), nullable=False),
    sa.Column("paired_at", sa.String(), nullable=True),
    sa.Column("device_name", sa.String(), nullable=True),
)
sa.Table(
    "device_config",
    metadata,
    sa.Column("device_id", sa.String(), sa.ForeignKey("devices.device_id"), primary_key=True),
    sa.Column("interval", sa.Integer(), server_default=sa.text("15"), nullable=False),
    sa.Column("weather_location", sa.String(), server_default=sa.text("''"), nullable=False),
    sa.Column("postcode", sa.String(), server_default=sa.text("''"), nullable=False),
    sa.Column("spotify_enabled", sa.Boolean(), server_default=sa.text("0"), nullable=False),
    sa.Column("tfl_stop_ids", sa.JSON(), server_default=sa.text("'[]'"), nullable=False),
)
sa.Table(
    "frames",
    metadata,
    sa.Column("device_id", sa.String(), sa.ForeignKey("devices.device_id"), primary_key=True),
    sa.Column("frame", sa.LargeBinary(), nullable=False),
    sa.Column("etag", sa.String(), nullable=False),
    sa.Column("rendered_at", sa.String(), nullable=False),
)
sa.Table(
    "glowmarkt_credentials",
    metadata,
    sa.Column("device_id", sa.String(), sa.ForeignKey("devices.device_id"), primary_key=True),
    sa.Column("username", sa.String(), server_default=sa.text("''"), nullable=False),
    sa.Column("password_encrypted", sa.String(), nullable=True),
)
sa.Table(
    "pairing_codes",
    metadata,
    sa.Column("code", sa.String(), primary_key=True),
    sa.Column("device_id", sa.String(), sa.ForeignKey("devices.device_id"), nullable=False),
    sa.Column("expires_at", sa.String(), nullable=False),
)
sa.Table(
    "spotify_tokens",
    metadata,
    sa.Column("device_id", sa.String(), sa.ForeignKey("devices.device_id"), primary_key=True),
    sa.Column("refresh_token", sa.String(), nullable=False),
    sa.Column("access_token", sa.String(), nullable=True),
    sa.Column("expires_at", sa.String(), nullable=True),
)

# The pre-Alembic schema, which a deploy applied on every start: a DB that skipped some
# releases may be missing whole tables, which this fills in before the tables are rebuilt.
LEGACY_TABLES = [
    """CREATE TABLE IF NOT EXISTS devices (
        device_id TEXT PRIMARY KEY, device_secret_hash TEXT NOT NULL, created_at TEXT NOT NULL,
        paired_at TEXT, device_name TEXT)""",
    """CREATE TABLE IF NOT EXISTS pairing_codes (
        code TEXT PRIMARY KEY, device_id TEXT NOT NULL REFERENCES devices(device_id), expires_at TEXT NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS device_config (
        device_id TEXT PRIMARY KEY REFERENCES devices(device_id), interval INTEGER NOT NULL DEFAULT 15,
        weather_location TEXT NOT NULL DEFAULT '', postcode TEXT NOT NULL DEFAULT '',
        spotify_enabled INTEGER NOT NULL DEFAULT 0, tfl_stop_ids TEXT NOT NULL DEFAULT '[]')""",
    """CREATE TABLE IF NOT EXISTS spotify_tokens (
        device_id TEXT PRIMARY KEY REFERENCES devices(device_id), refresh_token TEXT NOT NULL,
        access_token TEXT, expires_at TEXT)""",
    """CREATE TABLE IF NOT EXISTS glowmarkt_credentials (
        device_id TEXT PRIMARY KEY REFERENCES devices(device_id), username TEXT NOT NULL DEFAULT '',
        password_encrypted TEXT)""",
    """CREATE TABLE IF NOT EXISTS frames (
        device_id TEXT PRIMARY KEY REFERENCES devices(device_id), frame BLOB NOT NULL, etag TEXT NOT NULL,
        rendered_at TEXT NOT NULL)""",
]


def _columns(bind, table: str) -> set[str]:
    return {column["name"] for column in sa.inspect(bind).get_columns(table)}


def _apply_legacy_column_additions(bind) -> None:
    """The hand-rolled column additions that predate Alembic."""
    columns = _columns(bind, "devices")
    if "paired_at" not in columns:
        op.add_column("devices", sa.Column("paired_at", sa.String(), nullable=True))
        # A device with an unredeemed code is mid-pairing; any other existing device is treated as paired.
        op.execute(
            "UPDATE devices SET paired_at = created_at WHERE device_id NOT IN (SELECT device_id FROM pairing_codes)"
        )
    if "device_name" not in columns:
        op.add_column("devices", sa.Column("device_name", sa.String(), nullable=True))
    if "postcode" not in _columns(bind, "device_config"):
        op.add_column("device_config", sa.Column("postcode", sa.String(), server_default=sa.text("''"), nullable=False))


def upgrade() -> None:
    bind = op.get_bind()
    if "devices" not in sa.inspect(bind).get_table_names():
        metadata.create_all(bind)
        return

    for statement in LEGACY_TABLES:
        op.execute(statement)
    _apply_legacy_column_additions(bind)
    for table in metadata.sorted_tables:
        with op.batch_alter_table(table.name, copy_from=table, recreate="always"):
            pass


def downgrade() -> None:
    metadata.drop_all(op.get_bind())
