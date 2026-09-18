import json
from datetime import datetime, timezone
from typing import Any

import requests
from flask import Blueprint, current_app, jsonify, redirect, render_template, request, session, url_for

from .auth import require_paired_session
from .db import get_db

bp = Blueprint("web", __name__)

TFL_API_BASE = "https://api.tfl.gov.uk"


@bp.get("/")
def index():
    return redirect(url_for("web.device_config") if "device_id" in session else url_for("web.pair"))


@bp.route("/pair", methods=["GET", "POST"])
def pair():
    if request.method == "GET":
        return render_template("pair.html", error=None)

    code = request.form.get("code", "").strip().upper()
    db = get_db()
    row = db.execute(
        "SELECT device_id, expires_at FROM pairing_codes WHERE code = ?", (code,)
    ).fetchone()

    if row is None or datetime.fromisoformat(row["expires_at"]) < datetime.now(timezone.utc):
        return render_template("pair.html", error="That code is invalid or has expired.")

    db.execute("DELETE FROM pairing_codes WHERE code = ?", (code,))
    db.commit()

    session.clear()
    session.permanent = True
    session["device_id"] = row["device_id"]
    return redirect(url_for("web.device_config"))


@bp.route("/device", methods=["GET", "POST"])
@require_paired_session
def device_config():
    device_id = session["device_id"]
    db = get_db()

    if request.method == "POST":
        raw_stops = request.form.get("stops_order", "")
        stop_ids = [s.strip() for s in raw_stops.split(",") if s.strip()]

        interval_val = request.form.get("interval", "15").strip()
        interval = int(interval_val) if interval_val.isdigit() and int(interval_val) > 0 else 15

        weather_location = request.form.get("weather_location", "").strip()
        spotify_enabled = request.form.get("spotify_enabled") == "on"

        db.execute(
            """
            UPDATE device_config
            SET interval = ?, weather_location = ?, spotify_enabled = ?, tfl_stop_ids = ?
            WHERE device_id = ?
            """,
            (interval, weather_location, int(spotify_enabled), json.dumps(stop_ids), device_id),
        )
        db.commit()
        return redirect(url_for("web.device_config"))

    config = db.execute("SELECT * FROM device_config WHERE device_id = ?", (device_id,)).fetchone()
    stop_ids = json.loads(config["tfl_stop_ids"])

    http = requests.Session()
    resolved_stops = [
        _resolve_stop_info(stop_id, http)
        or {"id": stop_id, "name": stop_id, "mode": "unknown", "letter": "", "lines": [], "line_count": 0}
        for stop_id in stop_ids
    ]

    spotify_linked = (
        db.execute("SELECT 1 FROM spotify_tokens WHERE device_id = ?", (device_id,)).fetchone() is not None
    )

    return render_template(
        "device.html", device_id=device_id, config=config, stops=resolved_stops, spotify_linked=spotify_linked
    )


@bp.post("/device/spotify/disconnect")
@require_paired_session
def spotify_disconnect():
    db = get_db()
    db.execute("DELETE FROM spotify_tokens WHERE device_id = ?", (session["device_id"],))
    db.commit()
    return redirect(url_for("web.device_config"))


def _tfl_params() -> dict[str, str]:
    app_key = current_app.config["TFL_APP_KEY"]
    return {"app_key": app_key} if app_key else {}


def _resolve_stop_info(stop_id: str, http: requests.Session) -> dict[str, Any] | None:
    """Fetch details for a specific NaPTAN ID and format it for the UI."""
    try:
        r = http.get(f"{TFL_API_BASE}/StopPoint/{stop_id}", params=_tfl_params(), timeout=5)
        if r.status_code != 200:
            return None
        data = r.json()

        def search_node(node):
            nid = node.get("id") or node.get("naptanId")
            if nid == stop_id:
                return node
            for child in node.get("children", []):
                found = search_node(child)
                if found:
                    return found
            return None

        target = search_node(data) or data
        stop_type = target.get("stopType")
        common_name = target.get("commonName", stop_id)
        modes = target.get("modes", [])
        letter = target.get("stopLetter") or target.get("indicator") or ""
        lines = [line.get("name") for line in target.get("lines", []) if line.get("name")]

        is_tube = "tube" in modes or stop_type == "NaptanMetroStation"
        clean_name = common_name.removesuffix(" Underground Station").strip()

        return {
            "id": stop_id,
            "name": clean_name,
            "mode": "tube" if is_tube else "bus",
            "letter": letter,
            "lines": lines[:8],
            "line_count": len(lines),
        }
    except Exception as e:
        print(f"Error resolving stop {stop_id}: {e}")
        return None


@bp.get("/api/tfl/search")
def search_stops():
    """Searches TfL API for stops and extracts selectable Tube stations and Bus stops."""
    query = request.args.get("q", "").strip()
    if not query or len(query) < 2:
        return jsonify([])

    try:
        http = requests.Session()
        params = {"modes": "tube,bus", "maxResults": "15", **_tfl_params()}
        res = http.get(f"{TFL_API_BASE}/StopPoint/Search/{query}", params=params, timeout=6)
        res.raise_for_status()
        matches = res.json().get("matches", [])

        results = []
        seen: set[str] = set()

        for match in matches[:8]:
            mid = match.get("id")
            try:
                sp_res = http.get(f"{TFL_API_BASE}/StopPoint/{mid}", params=_tfl_params(), timeout=5)
                if sp_res.status_code != 200:
                    continue
                sp = sp_res.json()

                def extract(node):
                    nid = node.get("id") or node.get("naptanId")
                    if not nid or nid in seen:
                        return
                    st = node.get("stopType")

                    if st == "NaptanMetroStation":
                        seen.add(nid)
                        lines = [line.get("name") for line in node.get("lines", []) if line.get("name")]
                        name = node.get("commonName", "").removesuffix(" Underground Station").strip()
                        results.append({
                            "id": nid,
                            "name": name,
                            "mode": "tube",
                            "letter": "",
                            "lines": lines,
                            "subtitle": f"Underground • {', '.join(lines) if lines else 'All lines'}",
                        })
                    elif st == "NaptanPublicBusCoachTram":
                        seen.add(nid)
                        letter = node.get("stopLetter") or node.get("indicator") or ""
                        lines = [line.get("name") for line in node.get("lines", []) if line.get("name")]
                        subtitle = f"Stop {letter} • {', '.join(lines[:6])}" if letter else f"Bus • {', '.join(lines[:6])}"
                        if len(lines) > 6:
                            subtitle += f" (+{len(lines) - 6} more)"
                        results.append({
                            "id": nid,
                            "name": node.get("commonName", ""),
                            "mode": "bus",
                            "letter": letter,
                            "lines": lines,
                            "subtitle": subtitle,
                        })

                    for child in node.get("children", []):
                        extract(child)

                extract(sp)
            except Exception as ex:
                print(f"Error parsing search result {mid}: {ex}")

        return jsonify(results)
    except Exception as e:
        print(f"TfL Search API error: {e}")
        return jsonify([]), 500
