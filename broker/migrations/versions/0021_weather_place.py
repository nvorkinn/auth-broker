"""Add the picked weather place: device_config.weather_place_id, _name, _label, weather_country_code, _lat, _lon.

Existing configs keep their free-text weather_location and get no place: `flask weather backfill` looks theirs up
with Open-Meteo, which a migration shouldn't call.

Revision ID: 0021
Revises: 0020
Create Date: 2026-10-17 12:00:00.000000

"""

from typing import TYPE_CHECKING

import sqlalchemy as sa
from alembic import op

if TYPE_CHECKING:
    from collections.abc import Sequence

# revision identifiers, used by Alembic.
revision: str = "0021"
down_revision: str | Sequence[str] | None = "0020"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Alembic looks these up by name, so export them rather than leave them looking unused.
__all__ = ["branch_labels", "depends_on", "down_revision", "downgrade", "revision", "upgrade"]

COLUMNS = (
    ("weather_place_id", sa.Integer),
    ("weather_place_name", sa.String),
    ("weather_place_label", sa.String),
    ("weather_country_code", sa.String),
    ("weather_lat", sa.Float),
    ("weather_lon", sa.Float),
)


def upgrade() -> None:
    """Add the place's columns, empty for every device."""
    with op.batch_alter_table("device_config") as batch:
        for name, type_ in COLUMNS:
            batch.add_column(sa.Column(name, type_(), nullable=True))


def downgrade() -> None:
    """Drop the place's columns; weather_location still names it."""
    with op.batch_alter_table("device_config") as batch:
        for name, _ in reversed(COLUMNS):
            batch.drop_column(name)
