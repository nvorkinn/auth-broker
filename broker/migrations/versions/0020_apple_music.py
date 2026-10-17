"""Add Apple Music: device_config.apple_music_enabled, apple_music_tokens for the listener's token, apple_music_plays.

Revision ID: 0020
Revises: 0019
Create Date: 2026-10-17 03:30:00.000000

"""

from typing import TYPE_CHECKING

import sqlalchemy as sa
from alembic import op

if TYPE_CHECKING:
    from collections.abc import Sequence

# revision identifiers, used by Alembic.
revision: str = "0020"
down_revision: str | Sequence[str] | None = "0019"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Alembic looks these up by name, so export them rather than leave them looking unused.
__all__ = ["branch_labels", "depends_on", "down_revision", "downgrade", "revision", "upgrade"]


def upgrade() -> None:
    """Add the Apple Music switch, off for every device, and the tables for Apple Music's token and plays."""
    with op.batch_alter_table("device_config") as batch:
        batch.add_column(sa.Column("apple_music_enabled", sa.Boolean(), nullable=False, server_default=sa.text("0")))
    op.create_table(
        "apple_music_tokens",
        sa.Column("device_id", sa.String(), sa.ForeignKey("devices.device_id"), primary_key=True),
        sa.Column("token_encrypted", sa.String(), nullable=True),
        sa.Column("seen_ids", sa.JSON(), nullable=False, server_default=sa.text("'[]'")),
    )
    op.create_table(
        "apple_music_plays",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("device_id", sa.String(), sa.ForeignKey("devices.device_id"), nullable=False),
        sa.Column("played_at", sa.DateTime(), nullable=False),
        sa.Column("song_id", sa.String(), nullable=False),
        sa.Column("artist", sa.String(), nullable=False, server_default=sa.text("''")),
        sa.Column("track", sa.JSON(), nullable=False),
    )
    op.create_index("ix_apple_music_plays_device_id", "apple_music_plays", ["device_id"])
    op.create_index("ix_apple_music_plays_played_at", "apple_music_plays", ["played_at"])


def downgrade() -> None:
    """Drop Apple Music's plays and token, and its switch."""
    op.drop_table("apple_music_plays")
    op.drop_table("apple_music_tokens")
    with op.batch_alter_table("device_config") as batch:
        batch.drop_column("apple_music_enabled")
