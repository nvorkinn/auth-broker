"""Keep every screen's refresh interval between 20 seconds and 5 minutes, and make 20 the default.

Revision ID: 0014
Revises: 0013
Create Date: 2026-10-16 18:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0014"
down_revision: str | Sequence[str] | None = "0013"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

__all__ = ["revision", "down_revision", "branch_labels", "depends_on", "upgrade", "downgrade"]


def upgrade() -> None:
    op.execute("UPDATE device_config SET interval = 20 WHERE interval < 20")
    op.execute("UPDATE device_config SET interval = 300 WHERE interval > 300")
    with op.batch_alter_table("device_config") as batch:
        batch.alter_column("interval", existing_type=sa.Integer(), server_default=sa.text("20"))


def downgrade() -> None:
    # The clamped values aren't restored: they're all valid under the old range too.
    with op.batch_alter_table("device_config") as batch:
        batch.alter_column("interval", existing_type=sa.Integer(), server_default=sa.text("15"))
