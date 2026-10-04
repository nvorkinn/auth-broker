"""A read-only view of every device, for monitoring (e.g. a Home Assistant RESTful sensor).
It never changes anything: resetting or deleting a device stays with the admin CLI."""

from datetime import datetime

from flask import Blueprint, jsonify
from sqlalchemy import select

from ..auth import require_status_token
from ..db import db
from ..models import Device
from ..services.setup import setup_missing

bp = Blueprint("status", __name__, url_prefix="/api/status")


def _timestamp(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


@bp.get("/devices")
@require_status_token
def devices():
    """Every device keyed by its id, so a sensor can pick one out with value_json["<device_id>"]."""
    all_devices = db.session.scalars(select(Device).order_by(Device.created_at)).all()
    return jsonify(
        {
            device.device_id: {
                "device_name": device.device_name,
                "created_at": _timestamp(device.created_at),
                "paired_at": _timestamp(device.paired_at),
                "last_seen_at": _timestamp(device.last_seen_at),
                "setup_missing": setup_missing(device.config) if device.config else None,
            }
            for device in all_devices
        }
    )
