"""Add the screen's language, keeping existing devices in English.

Revision ID: 0013
Revises: 0012
Create Date: 2026-10-16 17:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0013"
down_revision: str | Sequence[str] | None = "0012"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

__all__ = ["revision", "down_revision", "branch_labels", "depends_on", "upgrade", "downgrade"]


def upgrade() -> None:
    with op.batch_alter_table("device_config") as batch:
        batch.add_column(sa.Column("language", sa.String(), nullable=False, server_default=sa.text("'en'")))


def downgrade() -> None:
    with op.batch_alter_table("device_config") as batch:
        batch.drop_column("language")
