"""Add device_config.spotify_style, how the screen draws Spotify (one of SPOTIFY_STYLES).

Existing devices keep the look they have, "classic".

Revision ID: 0011
Revises: 0010
Create Date: 2026-10-16 16:00:00.000000

"""

from typing import TYPE_CHECKING

import sqlalchemy as sa
from alembic import op

if TYPE_CHECKING:
    from collections.abc import Sequence

# revision identifiers, used by Alembic.
revision: str = "0011"
down_revision: str | Sequence[str] | None = "0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Alembic looks these up by name, so export them rather than leave them looking unused.
__all__ = ["branch_labels", "depends_on", "down_revision", "downgrade", "revision", "upgrade"]


def upgrade() -> None:
    """Add device_config.spotify_style."""
    with op.batch_alter_table("device_config") as batch:
        batch.add_column(sa.Column("spotify_style", sa.String(), nullable=False, server_default=sa.text("'classic'")))


def downgrade() -> None:
    """Drop device_config.spotify_style."""
    with op.batch_alter_table("device_config") as batch:
        batch.drop_column("spotify_style")
