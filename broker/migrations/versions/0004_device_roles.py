"""Give a device two independent secrets, one per role, and a pool where renderers and screens wait
to be matched.

devices.device_secret_hash becomes renderer_secret_hash, so every existing device keeps its secret
as its renderer. display_secret_hash is new and NULL for standalone Pis. Both are unique: a secret
alone identifies its device. pending_registrations holds renderers and screens that registered but
haven't been matched with the other role yet.

Downgrading keeps whatever is stored, so a device whose secret is stored as SHA-256 can't
authenticate on the old code and has to register again.

Revision ID: 0004
Revises: 0003
Create Date: 2026-10-05 20:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0004"
down_revision: str | Sequence[str] | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Alembic looks these up by name, so export them rather than leave them looking unused.
__all__ = ["revision", "down_revision", "branch_labels", "depends_on", "upgrade", "downgrade"]


def upgrade() -> None:
    with op.batch_alter_table("devices") as batch:
        batch.alter_column("device_secret_hash", new_column_name="renderer_secret_hash", existing_type=sa.String())
        batch.add_column(sa.Column("display_secret_hash", sa.String(), nullable=True))
    # Outside the batch: it can't index a column it's renaming in the same pass.
    op.create_index("ix_devices_renderer_secret_hash", "devices", ["renderer_secret_hash"], unique=True)
    op.create_index("ix_devices_display_secret_hash", "devices", ["display_secret_hash"], unique=True)

    op.create_table(
        "pending_registrations",
        sa.Column("secret_hash", sa.String(), primary_key=True),
        sa.Column("role", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("pending_registrations")
    op.drop_index("ix_devices_display_secret_hash", "devices")
    op.drop_index("ix_devices_renderer_secret_hash", "devices")
    with op.batch_alter_table("devices") as batch:
        batch.drop_column("display_secret_hash")
        batch.alter_column("renderer_secret_hash", new_column_name="device_secret_hash", existing_type=sa.String())
