from datetime import UTC, datetime
from hashlib import blake2b

from flask import Blueprint, Response, abort, request

from .auth import require_device_auth, require_renderer_auth
from .db import get_db

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
    db = get_db()
    if db.execute("SELECT 1 FROM devices WHERE device_id = ?", (device_id,)).fetchone() is None:
        abort(404)
    db.execute(
        """
        INSERT INTO frames (device_id, frame, etag, rendered_at)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(device_id) DO UPDATE SET
            frame = excluded.frame,
            etag = excluded.etag,
            rendered_at = excluded.rendered_at
        """,
        (device_id, frame, etag, now_iso()),
    )
    db.commit()
    return "", 204


@bp.get("/<device_id>/frame")
@require_device_auth
def get_frame(device_id):
    db = get_db()
    row = db.execute("SELECT frame, etag FROM frames WHERE device_id = ?", (device_id,)).fetchone()
    if not row:
        abort(404)
    resp = Response(row["frame"], mimetype="application/octet-stream")
    resp.set_etag(row["etag"])
    return resp.make_conditional(request)
