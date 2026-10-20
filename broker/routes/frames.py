"""A screen's frames: the screen polls for the one the render pool drew, and reports its logs and metrics."""

import json
import logging
import os
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import requests
from flask import Blueprint, Response, jsonify, request

from broker.auth import require_device_auth
from broker.services import frame_record, pace

if TYPE_CHECKING:
    from flask.typing import ResponseReturnValue

bp = Blueprint("frames", __name__, url_prefix="/api")

# How long a screen with no frame sleeps at most: the pool's first render of it (a screen just paired, or a Redis that
# restarted) lands within the minute.
NO_FRAME_RETRY_S = 10

# Where a screen's reports go: VictoriaLogs' JSON lines endpoint and VictoriaMetrics' InfluxDB line protocol endpoint,
# both on the host's loopback, which the broker shares through host networking.
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


@bp.post("/frame")
@require_device_auth()
def post_frame(device_id: str) -> ResponseReturnValue:
    """Serve the screen its current frame, and forward what it logged and measured since its last poll.

    The screen polls this, with If-None-Match once it has a frame: 200 with the frame, 304 if it has it, 404 with none
    yet. Every answer carries Retry-After (services/pace.py), so its loop is just "handle the status, sleep
    Retry-After". Its logs and metrics are forwarded without making it wait.
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
    shown = frame_record.head(device_id)
    sleep_s = pace.retry_after(device_id)
    if shown is None:
        return _no_frame(sleep_s)
    resp = Response(mimetype="application/octet-stream")
    resp.set_etag(shown.etag)
    resp.headers["Retry-After"] = str(sleep_s)
    # make_conditional only looks at GET and HEAD, so a POST answers its 304 itself.
    if request.if_none_match.contains_weak(shown.etag):
        return Response(status=304, headers={"ETag": resp.headers["ETag"], "Retry-After": resp.headers["Retry-After"]})
    # Only now the 48 KB, so a 304 never reads it.
    frame = frame_record.frame_of(device_id, shown.etag)
    if frame is None:
        # The record went, or changed, since its etag was read: serve it whole as it is now, if there is one.
        latest = frame_record.read(device_id)
        if latest is None:
            return _no_frame(sleep_s)
        resp.set_etag(latest.etag)
        frame = latest.frame
    resp.set_data(frame)
    return resp


def _no_frame(sleep_s: int) -> ResponseReturnValue:
    """Answer a screen that has no frame yet: 404, and ask again in at most NO_FRAME_RETRY_S."""
    return jsonify(error="no frame yet"), 404, {"Retry-After": str(min(sleep_s, NO_FRAME_RETRY_S))}
