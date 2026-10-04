from datetime import UTC, datetime
from hashlib import blake2b

from flask import Blueprint, Response, abort, request

from ..auth import require_device_auth, require_renderer_auth
from ..db import db
from ..models import Device, Frame

bp = Blueprint("frames", __name__, url_prefix="/api/frames")

FRAME_BYTES = 800 * 480 // 8  # 1-bit packed; better to share this via the protocol package


def now_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


@bp.put("/<device_id>/frame")
@require_renderer_auth
def update_frame(device_id):
    frame = request.get_data()
    if len(frame) != FRAME_BYTES:
        abort(400)
    etag = blake2b(frame, digest_size=8).hexdigest()
    if db.session.get(Device, device_id) is None:
        abort(404)
    row = db.session.get(Frame, device_id) or Frame(device_id=device_id)
    row.frame, row.etag, row.rendered_at = frame, etag, now_iso()
    db.session.add(row)
    db.session.commit()
    return "", 204


@bp.get("/<device_id>/frame")
@require_device_auth
def get_frame(device_id):
    row = db.session.get(Frame, device_id)
    if not row:
        abort(404)
    resp = Response(row.frame, mimetype="application/octet-stream")
    resp.set_etag(row.etag)
    return resp.make_conditional(request)
