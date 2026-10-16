"""Add the screen-wide clock format, keeping existing devices on 24-hour time.

Revision ID: 0011
Revises: 0010
Create Date: 2026-10-16 16:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0011"
down_revision: str | Sequence[str] | None = "0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

__all__ = ["revision", "down_revision", "branch_labels", "depends_on", "upgrade", "downgrade"]


def upgrade() -> None:
    with op.batch_alter_table("device_config") as batch:
        batch.add_column(sa.Column("clock_format", sa.String(), nullable=False, server_default=sa.text("'24h'")))


def downgrade() -> None:
    with op.batch_alter_table("device_config") as batch:
        batch.drop_column("clock_format")
