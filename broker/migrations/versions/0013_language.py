"""Add the screen's language, keeping existing devices in English.

Revision ID: 0013
Revises: 0012
Create Date: 2026-10-16 17:00:00.000000

"""

from typing import TYPE_CHECKING

import sqlalchemy as sa
from alembic import op

if TYPE_CHECKING:
    from collections.abc import Sequence

revision: str = "0013"
down_revision: str | Sequence[str] | None = "0012"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

__all__ = ["branch_labels", "depends_on", "down_revision", "downgrade", "revision", "upgrade"]


def upgrade() -> None:
    """Add device_config.language."""
    with op.batch_alter_table("device_config") as batch:
        batch.add_column(sa.Column("language", sa.String(), nullable=False, server_default=sa.text("'en'")))


def downgrade() -> None:
    """Drop device_config.language."""
    with op.batch_alter_table("device_config") as batch:
        batch.drop_column("language")
