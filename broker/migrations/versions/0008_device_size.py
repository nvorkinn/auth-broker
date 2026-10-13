"""Add device_config.width and height, the screen's size in pixels as it reported at registration.

Existing devices have none.

Revision ID: 0008
Revises: 0007
Create Date: 2026-10-14 00:20:00.000000

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
    with op.batch_alter_table("device_config") as batch:
        batch.add_column(sa.Column("width", sa.Integer(), nullable=True))
        batch.add_column(sa.Column("height", sa.Integer(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("device_config") as batch:
        batch.drop_column("height")
        batch.drop_column("width")
