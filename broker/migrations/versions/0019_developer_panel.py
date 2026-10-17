"""Add the developer panel: device_config.developer_enabled, and github_tokens for its GitHub token.

Revision ID: 0019
Revises: 0018
Create Date: 2026-10-17 01:30:00.000000

"""

from typing import TYPE_CHECKING

import sqlalchemy as sa
from alembic import op

if TYPE_CHECKING:
    from collections.abc import Sequence

# revision identifiers, used by Alembic.
revision: str = "0019"
down_revision: str | Sequence[str] | None = "0018"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Alembic looks these up by name, so export them rather than leave them looking unused.
__all__ = ["branch_labels", "depends_on", "down_revision", "downgrade", "revision", "upgrade"]


def upgrade() -> None:
    """Add the developer switch, off for every device, and the table for its GitHub token."""
    with op.batch_alter_table("device_config") as batch:
        batch.add_column(sa.Column("developer_enabled", sa.Boolean(), nullable=False, server_default=sa.text("0")))
    op.create_table(
        "github_tokens",
        sa.Column("device_id", sa.String(), sa.ForeignKey("devices.device_id"), primary_key=True),
        sa.Column("token_encrypted", sa.String(), nullable=False),
    )


def downgrade() -> None:
    """Drop the GitHub tokens and the switch."""
    op.drop_table("github_tokens")
    with op.batch_alter_table("device_config") as batch:
        batch.drop_column("developer_enabled")
