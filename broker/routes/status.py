"""A read-only view of every device, for monitoring (e.g. a Home Assistant RESTful sensor).

It never changes anything: resetting or deleting a device stays with the admin CLI.
"""

from typing import TYPE_CHECKING

from flask import Blueprint, jsonify
from sqlalchemy import select

from broker.auth import require_status_token
from broker.db import db
from broker.models import Device

if TYPE_CHECKING:
    from datetime import datetime

    from flask.typing import ResponseReturnValue

bp = Blueprint("status", __name__, url_prefix="/api/status")


def _timestamp(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


@bp.get("/devices")
@require_status_token
def devices() -> ResponseReturnValue:
    """List every device keyed by its id, so a sensor can pick one out with value_json["<device_id>"]."""
    all_devices = db.session.scalars(select(Device).order_by(Device.created_at)).all()
    return jsonify(
        {
            device.device_id: {
                "device_name": device.device_name,
                "created_at": _timestamp(device.created_at),
                "paired_at": _timestamp(device.paired_at),
                "last_seen_at": _timestamp(device.last_seen_at),
            }
            for device in all_devices
        }
    )
