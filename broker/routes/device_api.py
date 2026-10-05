"""Device endpoints without a device_id in the path: the bearer secret alone identifies the device
(see require_device_auth). They're what a client matched through the pending pool polls, as it may
not know its device_id yet: both answer 202 while it waits."""

from flask import Blueprint

from . import devices, frames

bp = Blueprint("device_api", __name__, url_prefix="/api")

bp.add_url_rule("/config", view_func=devices.get_config)
bp.add_url_rule("/frame", view_func=frames.get_frame)
