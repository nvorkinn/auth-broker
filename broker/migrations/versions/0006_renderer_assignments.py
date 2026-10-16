"""A screen becomes a device the moment it registers, and gets a renderer assigned to it.

devices.renderer_secret_hash is now NULL until the device's renderer registers, because a screen
exists (and can be paired and named) before its renderer is started. devices.display_last_seen_at
is when the screen itself last made a request. renderer_assignments is what the orchestrator turns
into running renderers.

Revision ID: 0006
Revises: 0005
Create Date: 2026-10-13 12:00:00.000000

"""

from typing import TYPE_CHECKING

import sqlalchemy as sa
from alembic import op

if TYPE_CHECKING:
    from collections.abc import Sequence

# revision identifiers, used by Alembic.
revision: str = "0006"
down_revision: str | Sequence[str] | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Alembic looks these up by name, so export them rather than leave them looking unused.
__all__ = ["branch_labels", "depends_on", "down_revision", "downgrade", "revision", "upgrade"]


def upgrade() -> None:
    """Let a device exist without a renderer, and add renderer_assignments."""
    # Screens no longer wait in the pool; any that were do the usual: get a 401 and register again.
    op.execute("DELETE FROM pending_registrations WHERE role = 'display'")
    with op.batch_alter_table("devices") as batch:
        batch.alter_column("renderer_secret_hash", existing_type=sa.String(), nullable=True)
        batch.add_column(sa.Column("display_last_seen_at", sa.DateTime(), nullable=True))

    op.create_table(
        "renderer_assignments",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("device_id", sa.String(), sa.ForeignKey("devices.device_id"), nullable=True, unique=True),
        sa.Column("slug", sa.String(), nullable=False, unique=True),
        sa.Column("claim_token", sa.String(), nullable=True, unique=True),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("detail", sa.String(), nullable=False, server_default=sa.text("''")),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )


def downgrade() -> None:
    """Drop renderer_assignments and require a renderer again."""
    op.drop_table("renderer_assignments")
    with op.batch_alter_table("devices") as batch:
        batch.drop_column("display_last_seen_at")
        # A device with no renderer yet can't be kept: the old schema requires one.
        batch.alter_column("renderer_secret_hash", existing_type=sa.String(), nullable=False)
