from datetime import UTC, datetime
from hashlib import blake2b

from flask import Blueprint, Response, abort, jsonify, request
from sqlalchemy.dialects.sqlite import insert

from ..auth import require_device_auth
from ..db import db
from ..models import DeviceConfig, Frame

bp = Blueprint("frames", __name__, url_prefix="/api/frames")

FRAME_BYTES = 800 * 480 // 8  # 1-bit packed; better to share this via the protocol package
# Retry-After, in seconds, on a GET before the renderer's first frame: it's expected any moment.
NO_FRAME_RETRY_AFTER = 5


@bp.put("/<device_id>/frame")
@require_device_auth(roles={"renderer"})
def update_frame(device_id):
    frame = request.get_data()
    if len(frame) != FRAME_BYTES:
        abort(400)
    etag = blake2b(frame, digest_size=8).hexdigest()
    # rendered_at moves even when the frame is unchanged: it says the renderer is still alive.
    stmt = insert(Frame).values(device_id=device_id, frame=frame, etag=etag, rendered_at=datetime.now(UTC))
    stmt = stmt.on_conflict_do_update(
        index_elements=[Frame.device_id],
        set_={"frame": stmt.excluded.frame, "etag": stmt.excluded.etag, "rendered_at": stmt.excluded.rendered_at},
    )
    db.session.execute(stmt)
    db.session.commit()
    return "", 204


@bp.get("/<device_id>/frame")
@require_device_auth(roles={"display", "renderer"})
def get_frame(device_id):
    """Polled by the screen. Every answer it should keep polling after carries Retry-After, so its
    loop is just "handle the status, sleep Retry-After": the device's refresh interval once there's
    a frame (the renderer redraws no more often than that), a few seconds before the first one."""
    row = db.session.get(Frame, device_id)
    if not row:
        return jsonify(error="no frame yet"), 404, {"Retry-After": str(NO_FRAME_RETRY_AFTER)}
    resp = Response(row.frame, mimetype="application/octet-stream")
    resp.set_etag(row.etag)
    resp.headers["Retry-After"] = str(db.session.get(DeviceConfig, device_id).interval)
    return resp.make_conditional(request)
