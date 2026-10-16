"""Draw every panel in one style: device_config.style replaces tfl_style, spotify_style and notices_style.

Each device keeps the look of its departures, which it has had longest: grouped becomes airy, its new name. A new
device starts on "hero", the renderer's default.

Revision ID: 0017
Revises: 0016
Create Date: 2026-10-16 19:00:00.000000

"""

from typing import TYPE_CHECKING

import sqlalchemy as sa
from alembic import op

if TYPE_CHECKING:
    from collections.abc import Sequence

# revision identifiers, used by Alembic.
revision: str = "0017"
down_revision: str | Sequence[str] | None = "0016"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Alembic looks these up by name, so export them rather than leave them looking unused.
__all__ = ["branch_labels", "depends_on", "down_revision", "downgrade", "revision", "upgrade"]

OLD_COLUMNS = ("tfl_style", "spotify_style", "notices_style")


def upgrade() -> None:
    """Add device_config.style, taken from the departures' style, and drop the panels' own."""
    with op.batch_alter_table("device_config") as batch:
        batch.add_column(sa.Column("style", sa.String(), nullable=False, server_default=sa.text("'hero'")))
    op.execute(
        "UPDATE device_config SET style = CASE tfl_style WHEN 'grouped' THEN 'airy' WHEN 'card' THEN 'card' "
        "ELSE 'hero' END"
    )
    with op.batch_alter_table("device_config") as batch:
        for column in OLD_COLUMNS:
            batch.drop_column(column)


def downgrade() -> None:
    """Give every panel its own style again, each set to the one style (airy is grouped for the departures)."""
    with op.batch_alter_table("device_config") as batch:
        for column in OLD_COLUMNS:
            batch.add_column(sa.Column(column, sa.String(), nullable=False, server_default=sa.text("'hero'")))
    op.execute(
        "UPDATE device_config SET tfl_style = CASE style WHEN 'airy' THEN 'grouped' ELSE style END, "
        "spotify_style = style, notices_style = style"
    )
    with op.batch_alter_table("device_config") as batch:
        batch.drop_column("style")
