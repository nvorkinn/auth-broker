"""Add device_config.weather_design, how the weather is drawn on an HD screen.

Existing devices are backfilled with columns, the contract's default, which is what a renderer draws when it isn't
sent one.

Revision ID: 0026
Revises: 0025
Create Date: 2026-10-23 03:30:00.000000

"""

from typing import TYPE_CHECKING

import sqlalchemy as sa
from alembic import op

if TYPE_CHECKING:
    from collections.abc import Sequence

# revision identifiers, used by Alembic.
revision: str = "0026"
down_revision: str | Sequence[str] | None = "0025"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Alembic looks these up by name, so export them rather than leave them looking unused.
__all__ = ["branch_labels", "depends_on", "down_revision", "downgrade", "revision", "upgrade"]


def upgrade() -> None:
    """Add device_config.weather_design."""
    with op.batch_alter_table("device_config") as batch:
        batch.add_column(sa.Column("weather_design", sa.String(), nullable=False, server_default=sa.text("'columns'")))


def downgrade() -> None:
    """Drop device_config.weather_design."""
    with op.batch_alter_table("device_config") as batch:
        batch.drop_column("weather_design")
