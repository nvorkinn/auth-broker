"""A screen's frames: the renderer uploads them, and the screen polls for them and reports its logs and metrics."""

import json
import logging
import os
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from hashlib import blake2b
from typing import TYPE_CHECKING

import requests
from flask import Blueprint, Response, abort, jsonify, request
from sqlalchemy.dialects.sqlite import insert

from broker.auth import require_device_auth
from broker.db import db
from broker.models import Frame
from broker.services import pace

if TYPE_CHECKING:
    from flask.typing import ResponseReturnValue

bp = Blueprint("frames", __name__, url_prefix="/api")

FRAME_BYTES = 800 * 480 // 8  # 1-bit packed; better to share this via the protocol package

# Where a screen's reports go. Logs: VictoriaLogs' JSON lines endpoint, with the fields the dashboards
# group by (see the README's Logs section). Telemetry: VictoriaMetrics' InfluxDB line protocol endpoint.
# Both listen on the host's loopback, which the broker shares through host networking in compose.
FORWARD_TIMEOUT = 2
LOGS_URL_DEFAULT = "http://127.0.0.1:9428/insert/jsonline?_stream_fields=service,host&_msg_field=_msg&_time_field=_time"
METRICS_URL_DEFAULT = "http://127.0.0.1:8428/write"
# What the screen writes where its device_id goes in its line protocol; it can't know it itself.
DEVICE_ID_PLACEHOLDER = "{device_id}"
DEVICE_LOG_SERVICE = "countdown-esp32"
# Names a screen's log entry may carry its text under, in order of preference.
MESSAGE_KEYS = ("_msg", "log", "message", "msg")
# Set by the broker from what it knows, so a screen can't claim them.
RESERVED_KEYS = {"service", "host", "device_id", "level", *MESSAGE_KEYS}
LEVELS = {"warning": "warn", "critical": "fatal", "err": "error", "trace": "debug"}
KNOWN_LEVELS = {"debug", "info", "warn", "error", "fatal"}

logger = logging.getLogger(__name__)
_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="forward")


@bp.put("/frame")
@require_device_auth(roles={"renderer"})
def update_frame(device_id: str) -> ResponseReturnValue:
    """Store the renderer's latest frame for its screen."""
    frame = request.get_data()
    if len(frame) != FRAME_BYTES:
        abort(400)
    etag = blake2b(frame, digest_size=8).hexdigest()
    # rendered_at moves even when the frame is unchanged: it says the renderer is still alive.
    values = {"device_id": device_id, "frame": frame, "etag": etag, "rendered_at": datetime.now(UTC)}
    # Every upload restates whether a provider's data is bursty: no header means it isn't (any more).
    pace.store_hint(values, pace.parse_hint(request.headers.get(pace.HINT_HEADER)))
    stmt = insert(Frame).values(**values)
    stmt = stmt.on_conflict_do_update(
        index_elements=[Frame.device_id],
        set_={key: stmt.excluded[key] for key in values if key != "device_id"},
    )
    db.session.execute(stmt)
    db.session.commit()
    return "", 204


@bp.get("/frame")
@require_device_auth(roles={"display", "renderer"})
def get_frame(device_id: str) -> ResponseReturnValue:
    """Serve the screen its current frame.

    The screen polls this. Every answer it should keep polling after carries Retry-After, so its loop is
    just "handle the status, sleep Retry-After": until just after the next render, before the first frame too (see
    services/pace.py, the only place a sleep is worked out).
    """
    return _frame_response(device_id)


@bp.post("/frame")
@require_device_auth(roles={"display", "renderer"})
def post_frame(device_id: str) -> ResponseReturnValue:
    """Answer as the GET does, and forward what the screen logged and measured since its last poll.

    The screen reports both here to save it a request. Both are forwarded without making the screen wait.
    """
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
            _forward(
                os.environ.get("VICTORIA_LOGS_URL", LOGS_URL_DEFAULT),
                data=_log_lines(logs, device_id, datetime.now(UTC)),
                headers={"Content-Type": "application/json"},
            )
    return _frame_response(device_id)


def _log_lines(logs: list, device_id: str, received_at: datetime) -> bytes:
    """Turn a screen's log entries into one JSON object per line, the way VictoriaLogs ingests them.

    The screen has no clock, only the milliseconds it has been up, so a line is dated by how long before this
    report it was written: the report arrived just after its newest line.
    """
    entries = [entry if isinstance(entry, dict) else {"_msg": str(entry)} for entry in logs]
    uptimes = [e["uptime_ms"] for e in entries if _is_number(e.get("uptime_ms"))]
    newest = max(uptimes, default=0)
    records = []
    for entry in entries:
        uptime = entry.get("uptime_ms")
        age_ms = newest - uptime if _is_number(uptime) else 0
        records.append(_log_record(entry, device_id, received_at - timedelta(milliseconds=age_ms)))
    return "\n".join(json.dumps(record) for record in records).encode()


def _is_number(value: object) -> bool:
    return isinstance(value, int | float) and not isinstance(value, bool)


def _log_record(entry: dict, device_id: str, at: datetime) -> dict:
    message = next((entry[k] for k in MESSAGE_KEYS if entry.get(k) not in (None, "")), "")
    level = str(entry.get("level", "info")).lower()
    level = LEVELS.get(level, level)
    return {
        **{k: v for k, v in entry.items() if k not in RESERVED_KEYS and not k.startswith("_")},
        "_time": at.isoformat(timespec="milliseconds"),
        "_msg": str(message),
        "level": level if level in KNOWN_LEVELS else "info",
        "service": DEVICE_LOG_SERVICE,
        "host": device_id,
        "device_id": device_id,
    }


def _forward(url: str, **request_kwargs: object) -> None:
    _executor.submit(_post, url, request_kwargs)


def _post(url: str, request_kwargs: dict[str, object]) -> None:
    # Best-effort: a destination that's down or unhappy must never fail or slow a screen's poll.
    try:
        requests.post(url, timeout=FORWARD_TIMEOUT, **request_kwargs).raise_for_status()
    except requests.RequestException:
        logger.warning("could not forward to %s", url, exc_info=True)


def _frame_response(device_id: str) -> ResponseReturnValue:
    row = db.session.get(Frame, device_id)
    retry_after = str(pace.retry_after(device_id, row))
    if not row:
        return jsonify(error="no frame yet"), 404, {"Retry-After": retry_after}
    resp = Response(row.frame, mimetype="application/octet-stream")
    resp.set_etag(row.etag)
    resp.headers["Retry-After"] = retry_after
    if request.method == "POST":
        # make_conditional only looks at GET and HEAD, so a POST has to answer 304 itself.
        if request.if_none_match.contains_weak(row.etag):
            resp = Response(
                status=304, headers={"ETag": resp.headers["ETag"], "Retry-After": resp.headers["Retry-After"]}
            )
        return resp
    return resp.make_conditional(request)
