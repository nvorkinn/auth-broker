"""Pair devices with a logged-in user instead of a browser session.

devices.owner is the Authentik user a device is paired with. devices.pairing_token is what the
owner's browser presents to pair with a device without a code; it is unique and NULL for devices
registered before this revision, which pair with a code as before.

Existing paired devices have no owner yet, so nobody can reach their settings until one is set:
`flask devices owner <device_id> <user id>`.

Revision ID: 0005
Revises: 0004
Create Date: 2026-10-11 12:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0005"
down_revision: str | Sequence[str] | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Alembic looks these up by name, so export them rather than leave them looking unused.
__all__ = ["revision", "down_revision", "branch_labels", "depends_on", "upgrade", "downgrade"]


def upgrade() -> None:
    with op.batch_alter_table("devices") as batch:
        batch.add_column(sa.Column("pairing_token", sa.String(), nullable=True))
        batch.add_column(sa.Column("owner", sa.String(), nullable=True))
    op.create_index("ix_devices_pairing_token", "devices", ["pairing_token"], unique=True)
    op.create_index("ix_devices_owner", "devices", ["owner"])


def downgrade() -> None:
    op.drop_index("ix_devices_owner", "devices")
    op.drop_index("ix_devices_pairing_token", "devices")
    with op.batch_alter_table("devices") as batch:
        batch.drop_column("owner")
        batch.drop_column("pairing_token")
