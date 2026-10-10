import logging
import os
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from hashlib import blake2b

import requests
from flask import Blueprint, Response, abort, jsonify, request
from sqlalchemy.dialects.sqlite import insert

from ..auth import require_device_auth
from ..db import db
from ..models import DeviceConfig, Frame

bp = Blueprint("frames", __name__, url_prefix="/api")

FRAME_BYTES = 800 * 480 // 8  # 1-bit packed; better to share this via the protocol package
# Retry-After, in seconds, on a GET before the renderer's first frame: it's expected any moment.
NO_FRAME_RETRY_AFTER = 5

# Where a screen's reports go. Logs: Fluent Bit's HTTP input (fluent-bit/fluent-bit.yaml). Telemetry:
# VictoriaMetrics' InfluxDB line protocol endpoint. Both listen on the host's loopback, which the
# broker shares through host networking in compose.
FORWARD_TIMEOUT = 2
LOGS_URL_DEFAULT = "http://127.0.0.1:9880/logs"
METRICS_URL_DEFAULT = "http://127.0.0.1:8428/write"
# What the screen writes where its device_id goes in its line protocol; it can't know it itself.
DEVICE_ID_PLACEHOLDER = "{device_id}"

logger = logging.getLogger(__name__)
_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="forward")


@bp.put("/frame")
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


@bp.get("/frame")
@require_device_auth(roles={"display", "renderer"})
def get_frame(device_id):
    """Polled by the screen. Every answer it should keep polling after carries Retry-After, so its
    loop is just "handle the status, sleep Retry-After": the device's refresh interval once there's
    a frame (the renderer redraws no more often than that), a few seconds before the first one."""
    return _frame_response(device_id)


@bp.post("/frame")
@require_device_auth(roles={"display", "renderer"})
def post_frame(device_id):
    """Same answer as the GET, but the screen also reports what it logged and measured since its
    last poll, to save it a request. Both are forwarded without making the screen wait."""
    body = request.get_json(silent=True)
    if isinstance(body, dict):
        metrics, logs = body.get("metrics"), body.get("logs")
        if isinstance(metrics, str) and metrics.strip():
            # replace, not str.format: braces elsewhere in the text must not be able to raise.
            lines = metrics.replace(DEVICE_ID_PLACEHOLDER, device_id)
            _forward(
                os.environ.get("VICTORIA_METRICS_URL", METRICS_URL_DEFAULT),
                data=lines.encode(),
                headers={"Content-Type": "text/plain"},
            )
        if isinstance(logs, list) and logs:
            records = [_log_record(entry, device_id) for entry in logs]
            _forward(os.environ.get("FLUENT_BIT_LOGS_URL", LOGS_URL_DEFAULT), json=records)
    return _frame_response(device_id)


def _log_record(entry, device_id: str) -> dict:
    """Fluent Bit's HTTP input only takes JSON objects; VictoriaLogs reads the text from `log`."""
    record = entry if isinstance(entry, dict) else {"log": str(entry)}
    return {**record, "device_id": device_id}


def _forward(url: str, **request_kwargs) -> None:
    _executor.submit(_post, url, request_kwargs)


def _post(url: str, request_kwargs: dict) -> None:
    # Best-effort: a destination that's down or unhappy must never fail or slow a screen's poll.
    try:
        requests.post(url, timeout=FORWARD_TIMEOUT, **request_kwargs).raise_for_status()
    except requests.RequestException:
        logger.warning("could not forward to %s", url, exc_info=True)


def _frame_response(device_id: str):
    row = db.session.get(Frame, device_id)
    if not row:
        return jsonify(error="no frame yet"), 404, {"Retry-After": str(NO_FRAME_RETRY_AFTER)}
    resp = Response(row.frame, mimetype="application/octet-stream")
    resp.set_etag(row.etag)
    resp.headers["Retry-After"] = str(db.session.get(DeviceConfig, device_id).interval)
    if request.method == "POST":
        # make_conditional only looks at GET and HEAD, so a POST has to answer 304 itself.
        if request.if_none_match.contains_weak(row.etag):
            resp = Response(
                status=304, headers={"ETag": resp.headers["ETag"], "Retry-After": resp.headers["Retry-After"]}
            )
        return resp
    return resp.make_conditional(request)
