"""Add device_config.notices_style, how the screen draws the notice board (one of NOTICE_STYLES).

Every device starts on "hero", the renderer's default.

Revision ID: 0015
Revises: 0014
Create Date: 2026-10-16 18:30:00.000000

"""

from typing import TYPE_CHECKING

import sqlalchemy as sa
from alembic import op

if TYPE_CHECKING:
    from collections.abc import Sequence

# revision identifiers, used by Alembic.
revision: str = "0015"
down_revision: str | Sequence[str] | None = "0014"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Alembic looks these up by name, so export them rather than leave them looking unused.
__all__ = ["branch_labels", "depends_on", "down_revision", "downgrade", "revision", "upgrade"]


def upgrade() -> None:
    """Add device_config.notices_style."""
    with op.batch_alter_table("device_config") as batch:
        batch.add_column(sa.Column("notices_style", sa.String(), nullable=False, server_default=sa.text("'hero'")))


def downgrade() -> None:
    """Drop device_config.notices_style."""
    with op.batch_alter_table("device_config") as batch:
        batch.drop_column("notices_style")
