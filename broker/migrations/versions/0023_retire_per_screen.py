"""Retire the per-screen renderers: no frames table, no renderer secrets, no claim tokens or orchestrator states.

Every screen is drawn by pi-dashboard's render pool, which writes its frame to Redis (`v1:frame:<device id>`), so
the broker keeps no copy of it (frames). With no renderer containers there's no renderer to authenticate
(devices.renderer_secret_hash), to claim its screen (renderer_assignments.claim_token), or for an orchestrator to
report on (renderer_assignments.status and detail). An assignment is now only a screen's name, its slug; those left
behind by forgotten devices (no device, waiting for the orchestrator to remove their containers) go.

The downgrade brings the columns and the table back empty: the frames are in Redis, and a renderer would have to be
claimed again.

Revision ID: 0023
Revises: 0022
Create Date: 2026-10-19 21:00:00.000000

"""

from typing import TYPE_CHECKING

import sqlalchemy as sa
from alembic import op

if TYPE_CHECKING:
    from collections.abc import Sequence

# revision identifiers, used by Alembic.
revision: str = "0023"
down_revision: str | Sequence[str] | None = "0022"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Alembic looks these up by name, so export them rather than leave them looking unused.
__all__ = ["branch_labels", "depends_on", "down_revision", "downgrade", "revision", "upgrade"]


def _rebuild_assignments(columns: list[sa.Column], copied: str, selected: str) -> None:
    """Rebuild renderer_assignments with `columns`, copying `copied` from the old table's `selected`.

    By hand rather than with a batch: a batch copies the table's constraints in an order of its own, which differs
    from one run to the next, and the schema is then not the same everywhere.
    """
    op.create_table(
        "renderer_assignments_new",
        *columns,
        sa.UniqueConstraint("device_id"),
        sa.ForeignKeyConstraint(["device_id"], ["devices.device_id"]),
        sa.UniqueConstraint("slug"),
        *([sa.UniqueConstraint("claim_token")] if any(c.name == "claim_token" for c in columns) else []),
    )
    op.execute(f"INSERT INTO renderer_assignments_new ({copied}) SELECT {selected} FROM renderer_assignments")  # noqa: S608 -- fixed column names
    op.drop_table("renderer_assignments")
    op.rename_table("renderer_assignments_new", "renderer_assignments")


def upgrade() -> None:
    """Drop the frames, the renderers' secrets and the orchestrator's columns."""
    op.drop_table("frames")
    op.execute("DELETE FROM renderer_assignments WHERE device_id IS NULL")
    _rebuild_assignments(
        [
            sa.Column("id", sa.String(), primary_key=True),
            sa.Column("device_id", sa.String(), nullable=True),
            sa.Column("slug", sa.String(), nullable=False),
            sa.Column("created_at", sa.DateTime(), nullable=False),
        ],
        "id, device_id, slug, created_at",
        "id, device_id, slug, created_at",
    )
    op.drop_index("ix_devices_renderer_secret_hash", "devices")
    with op.batch_alter_table("devices") as batch:
        batch.drop_column("renderer_secret_hash")


def downgrade() -> None:
    """Bring them back: no renderer secrets or frames, every assignment pending a renderer it hasn't a token for."""
    # Where it was, second, as the schema before 0004 depends on the columns' order.
    with op.batch_alter_table("devices", recreate="always") as batch:
        batch.add_column(sa.Column("renderer_secret_hash", sa.String(), nullable=True), insert_after="device_id")
    op.create_index("ix_devices_renderer_secret_hash", "devices", ["renderer_secret_hash"], unique=True)
    _rebuild_assignments(
        [
            sa.Column("id", sa.String(), primary_key=True),
            sa.Column("device_id", sa.String(), nullable=True),
            sa.Column("slug", sa.String(), nullable=False),
            sa.Column("claim_token", sa.String(), nullable=True),
            sa.Column("status", sa.String(), nullable=False),
            sa.Column("detail", sa.String(), nullable=False, server_default=sa.text("''")),
            sa.Column("created_at", sa.DateTime(), nullable=False),
        ],
        "id, device_id, slug, status, created_at",
        "id, device_id, slug, 'pending', created_at",
    )
    op.create_table(
        "frames",
        sa.Column("device_id", sa.String(), sa.ForeignKey("devices.device_id"), primary_key=True),
        sa.Column("frame", sa.LargeBinary(), nullable=False),
        sa.Column("etag", sa.String(), nullable=False),
        sa.Column("rendered_at", sa.DateTime(), nullable=False),
        sa.Column("pace", sa.Integer(), nullable=True),
        sa.Column("pace_until", sa.DateTime(), nullable=True),
    )
