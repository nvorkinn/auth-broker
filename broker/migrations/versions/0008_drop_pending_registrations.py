"""Drop pending_registrations: renderers no longer wait in a pool for a screen, they claim their
assignment's device with a claim token (POST /api/renderers/claim).

Revision ID: 0008
Revises: 0007
Create Date: 2026-10-14 00:30:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0008"
down_revision: str | Sequence[str] | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Alembic looks these up by name, so export them rather than leave them looking unused.
__all__ = ["revision", "down_revision", "branch_labels", "depends_on", "upgrade", "downgrade"]


def upgrade() -> None:
    op.drop_table("pending_registrations")


def downgrade() -> None:
    op.create_table(
        "pending_registrations",
        sa.Column("secret_hash", sa.String(), primary_key=True),
        sa.Column("role", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
