"""Add device_config.layout, which layout the screen is drawn in.

Nullable, and null for existing devices: null is the screen's default layout, which is what a renderer draws when it
isn't sent one.

Revision ID: 0027
Revises: 0026
Create Date: 2026-10-23 05:00:00.000000

"""

from typing import TYPE_CHECKING

import sqlalchemy as sa
from alembic import op

if TYPE_CHECKING:
    from collections.abc import Sequence

# revision identifiers, used by Alembic.
revision: str = "0027"
down_revision: str | Sequence[str] | None = "0026"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Alembic looks these up by name, so export them rather than leave them looking unused.
__all__ = ["branch_labels", "depends_on", "down_revision", "downgrade", "revision", "upgrade"]


def upgrade() -> None:
    """Add device_config.layout."""
    with op.batch_alter_table("device_config") as batch:
        batch.add_column(sa.Column("layout", sa.String(), nullable=True))


def downgrade() -> None:
    """Drop device_config.layout."""
    with op.batch_alter_table("device_config") as batch:
        batch.drop_column("layout")
