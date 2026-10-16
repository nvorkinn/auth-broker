"""Drop pending_registrations.

Renderers no longer wait in a pool for a screen, they claim their assignment's device with a claim token
(POST /api/renderers/claim).

Revision ID: 0008
Revises: 0007
Create Date: 2026-10-14 00:30:00.000000

"""

from typing import TYPE_CHECKING

import sqlalchemy as sa
from alembic import op

if TYPE_CHECKING:
    from collections.abc import Sequence

# revision identifiers, used by Alembic.
revision: str = "0008"
down_revision: str | Sequence[str] | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Alembic looks these up by name, so export them rather than leave them looking unused.
__all__ = ["branch_labels", "depends_on", "down_revision", "downgrade", "revision", "upgrade"]


def upgrade() -> None:
    """Drop pending_registrations."""
    op.drop_table("pending_registrations")


def downgrade() -> None:
    """Recreate pending_registrations, empty."""
    op.create_table(
        "pending_registrations",
        sa.Column("secret_hash", sa.String(), primary_key=True),
        sa.Column("role", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
