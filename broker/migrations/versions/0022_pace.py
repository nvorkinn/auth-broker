"""Pace the screen from the providers: frames.pace and pace_until, and no more device_config.interval.

The refresh interval is no longer the owner's to set (services/pace.py), so the column goes.

Revision ID: 0022
Revises: 0021
Create Date: 2026-10-17 18:00:00.000000

"""

from typing import TYPE_CHECKING

import sqlalchemy as sa
from alembic import op

if TYPE_CHECKING:
    from collections.abc import Sequence

# revision identifiers, used by Alembic.
revision: str = "0022"
down_revision: str | Sequence[str] | None = "0021"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Alembic looks these up by name, so export them rather than leave them looking unused.
__all__ = ["branch_labels", "depends_on", "down_revision", "downgrade", "revision", "upgrade"]


def upgrade() -> None:
    """Add the frame's pace request and drop the owner's interval."""
    with op.batch_alter_table("frames") as batch:
        batch.add_column(sa.Column("pace", sa.Integer(), nullable=True))
        batch.add_column(sa.Column("pace_until", sa.DateTime(), nullable=True))
    with op.batch_alter_table("device_config") as batch:
        batch.drop_column("interval")


def downgrade() -> None:
    """Bring the interval back at 60 s for everyone (what they get today by default), and drop the pace request."""
    with op.batch_alter_table("device_config") as batch:
        batch.add_column(sa.Column("interval", sa.Integer(), server_default=sa.text("60"), nullable=False))
    with op.batch_alter_table("frames") as batch:
        batch.drop_column("pace_until")
        batch.drop_column("pace")
