"""Add device_config.width and height, the screen's size in pixels as it reported at registration.

Existing devices are backfilled with 480x800 (vertical).

Revision ID: 0009
Revises: 0008
Create Date: 2026-10-14 00:20:00.000000

"""

from typing import TYPE_CHECKING

import sqlalchemy as sa
from alembic import op

if TYPE_CHECKING:
    from collections.abc import Sequence

# revision identifiers, used by Alembic.
revision: str = "0009"
down_revision: str | Sequence[str] | None = "0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Alembic looks these up by name, so export them rather than leave them looking unused.
__all__ = ["branch_labels", "depends_on", "down_revision", "downgrade", "revision", "upgrade"]


def upgrade() -> None:
    """Add device_config.width and height."""
    with op.batch_alter_table("device_config") as batch:
        batch.add_column(sa.Column("width", sa.Integer(), nullable=False, server_default=sa.text("480")))
        batch.add_column(sa.Column("height", sa.Integer(), nullable=False, server_default=sa.text("800")))


def downgrade() -> None:
    """Drop device_config.width and height."""
    with op.batch_alter_table("device_config") as batch:
        batch.drop_column("height")
        batch.drop_column("width")
