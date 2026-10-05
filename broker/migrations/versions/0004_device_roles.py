"""Give a device two independent secrets, one per role: renderer and display.

devices.device_secret_hash becomes renderer_secret_hash, so every existing device keeps its secret
as its renderer. display_secret_hash is new. Either may be NULL: a split deployment registers its
screen as a display first and attaches the server-side renderer later, or the other way round.

Downgrading can't keep a device with no renderer secret, as the old column is NOT NULL. Those
devices (and, through the foreign keys, their config, codes, tokens and frame) are deleted.

Revision ID: 0004
Revises: 0003
Create Date: 2026-10-05 20:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0004"
down_revision: str | Sequence[str] | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

DEPENDENT_TABLES = ["pairing_codes", "device_config", "spotify_tokens", "glowmarkt_credentials", "frames"]


def upgrade() -> None:
    with op.batch_alter_table("devices") as batch:
        batch.alter_column(
            "device_secret_hash", new_column_name="renderer_secret_hash", existing_type=sa.String(), nullable=True
        )
        batch.add_column(sa.Column("display_secret_hash", sa.String(), nullable=True))


def downgrade() -> None:
    orphans = "SELECT device_id FROM devices WHERE renderer_secret_hash IS NULL"
    for table in DEPENDENT_TABLES:
        op.execute(f"DELETE FROM {table} WHERE device_id IN ({orphans})")
    op.execute("DELETE FROM devices WHERE renderer_secret_hash IS NULL")
    with op.batch_alter_table("devices") as batch:
        batch.drop_column("display_secret_hash")
        batch.alter_column(
            "renderer_secret_hash", new_column_name="device_secret_hash", existing_type=sa.String(), nullable=False
        )
