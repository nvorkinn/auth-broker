from flask import Blueprint, session

from ..db import db
from ..models import Device

bp = Blueprint("logging_ui", __name__, url_prefix="/api/logging_ui")


@bp.get("/verify")
def verify():
    device = None
    if "device_id" in session:
        device = db.session.get(Device, session["device_id"])
    if device is None or device.paired_at is None:
        return "", 401
    else:
        return "", 200
