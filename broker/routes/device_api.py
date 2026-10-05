"""Device endpoints without a device_id in the path: the bearer secret alone identifies the device
(see require_device_auth), which logs each request with the device_id it found. A client matched
through the pending pool polls /config and /frame before it knows its device_id; both answer 202
while it waits. The rest let a renderer work without ever handling its device_id."""

from flask import Blueprint

from . import devices, frames, spotify_proxy

bp = Blueprint("device_api", __name__, url_prefix="/api")

bp.add_url_rule("/config", view_func=devices.get_config)
bp.add_url_rule("/frame", view_func=frames.get_frame)
bp.add_url_rule("/frame", view_func=frames.update_frame, methods=["PUT"])
bp.add_url_rule("/spotify/now-playing", view_func=spotify_proxy.now_playing)
bp.add_url_rule("/spotify/queue", view_func=spotify_proxy.queue)
bp.add_url_rule("/spotify/top/<any(artists, tracks):type_>", view_func=spotify_proxy.top)
