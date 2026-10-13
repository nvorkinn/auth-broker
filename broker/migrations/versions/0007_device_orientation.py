"""Add device_config.orientation, how the screen is mounted ("horizontal" or "vertical").

Existing devices start as vertical.

Revision ID: 0007
Revises: 0006
Create Date: 2026-10-13 22:15:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0007"
down_revision: str | Sequence[str] | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Alembic looks these up by name, so export them rather than leave them looking unused.
__all__ = ["revision", "down_revision", "branch_labels", "depends_on", "upgrade", "downgrade"]


def upgrade() -> None:
    with op.batch_alter_table("device_config") as batch:
        batch.add_column(sa.Column("orientation", sa.String(), nullable=False, server_default=sa.text("'vertical'")))


def downgrade() -> None:
    with op.batch_alter_table("device_config") as batch:
        batch.drop_column("orientation")
