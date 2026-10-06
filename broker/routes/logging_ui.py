from flask import Blueprint, session

from ..auth import require_paired_session
from ..db import db
from ..models import Device

bp = Blueprint("logging_ui", __name__, url_prefix="/api/logging_ui")


@bp.get("/verify")
@require_paired_session
def verify():
    device = None
    if "device_id" in session:
        device = db.session.get(Device, session["device_id"])
    if not device:
        return "", 200
    else:
        return "", 401
