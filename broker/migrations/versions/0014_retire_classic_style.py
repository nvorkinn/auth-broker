"""Retire the classic TfL and Spotify styles: devices drawn in classic move to hero, the new default.

Revision ID: 0014
Revises: 0013
Create Date: 2026-10-16 18:30:00.000000

"""

from typing import TYPE_CHECKING

import sqlalchemy as sa
from alembic import op

if TYPE_CHECKING:
    from collections.abc import Sequence

revision: str = "0014"
down_revision: str | Sequence[str] | None = "0013"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

__all__ = ["branch_labels", "depends_on", "down_revision", "downgrade", "revision", "upgrade"]

COLUMNS = ("tfl_style", "spotify_style")


def upgrade() -> None:
    """Move classic devices to hero, and make hero what a new device starts with."""
    for column in COLUMNS:
        op.execute(f"UPDATE device_config SET {column} = 'hero' WHERE {column} = 'classic'")  # noqa: S608 -- fixed names
    with op.batch_alter_table("device_config") as batch:
        for column in COLUMNS:
            batch.alter_column(column, existing_type=sa.String(), server_default=sa.text("'hero'"))


def downgrade() -> None:
    """Make classic the default again; devices stay on hero, which every broker since 0010 can serve."""
    with op.batch_alter_table("device_config") as batch:
        for column in COLUMNS:
            batch.alter_column(column, existing_type=sa.String(), server_default=sa.text("'classic'"))
