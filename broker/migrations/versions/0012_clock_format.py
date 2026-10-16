"""Add the screen-wide clock format, keeping existing devices on 24-hour time.

Revision ID: 0012
Revises: 0011
Create Date: 2026-10-16 16:00:00.000000

"""

from typing import TYPE_CHECKING

import sqlalchemy as sa
from alembic import op

if TYPE_CHECKING:
    from collections.abc import Sequence

revision: str = "0012"
down_revision: str | Sequence[str] | None = "0011"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

__all__ = ["branch_labels", "depends_on", "down_revision", "downgrade", "revision", "upgrade"]


def upgrade() -> None:
    """Add device_config.clock_format."""
    with op.batch_alter_table("device_config") as batch:
        batch.add_column(sa.Column("clock_format", sa.String(), nullable=False, server_default=sa.text("'24h'")))


def downgrade() -> None:
    """Drop device_config.clock_format."""
    with op.batch_alter_table("device_config") as batch:
        batch.drop_column("clock_format")
