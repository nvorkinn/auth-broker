"""Add devices.last_seen_at, when the device last made an authenticated request.

Existing devices start with NULL ("not seen since this was added") and get a value on their next
request.

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-05 18:00:00.000000

"""

from typing import TYPE_CHECKING

import sqlalchemy as sa
from alembic import op

if TYPE_CHECKING:
    from collections.abc import Sequence

# revision identifiers, used by Alembic.
revision: str = "0003"
down_revision: str | Sequence[str] | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Alembic looks these up by name, so export them rather than leave them looking unused.
__all__ = ["branch_labels", "depends_on", "down_revision", "downgrade", "revision", "upgrade"]


def upgrade() -> None:
    """Add devices.last_seen_at."""
    with op.batch_alter_table("devices") as batch:
        batch.add_column(sa.Column("last_seen_at", sa.DateTime(), nullable=True))


def downgrade() -> None:
    """Drop devices.last_seen_at."""
    with op.batch_alter_table("devices") as batch:
        batch.drop_column("last_seen_at")
