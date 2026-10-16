"""Start new devices on the airy style instead of hero.

Only the column's default changes: every existing device keeps the style it has.

Revision ID: 0018
Revises: 0017
Create Date: 2026-10-16 20:00:00.000000

"""

from typing import TYPE_CHECKING

import sqlalchemy as sa
from alembic import op

if TYPE_CHECKING:
    from collections.abc import Sequence

# revision identifiers, used by Alembic.
revision: str = "0018"
down_revision: str | Sequence[str] | None = "0017"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Alembic looks these up by name, so export them rather than leave them looking unused.
__all__ = ["branch_labels", "depends_on", "down_revision", "downgrade", "revision", "upgrade"]


def upgrade() -> None:
    """Make airy device_config.style's default."""
    with op.batch_alter_table("device_config") as batch:
        batch.alter_column("style", existing_type=sa.String(), server_default=sa.text("'airy'"))


def downgrade() -> None:
    """Make hero the default again."""
    with op.batch_alter_table("device_config") as batch:
        batch.alter_column("style", existing_type=sa.String(), server_default=sa.text("'hero'"))
