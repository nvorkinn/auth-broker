"""Browser-facing Apple Music linking.

The config page signs the listener in to Apple with MusicKit JS, which needs a developer token from here, and posts
back the Music User Token Apple hands it, which is stored against the screen.
"""

from typing import TYPE_CHECKING

from flask import Blueprint, current_app, g, jsonify, redirect, request, url_for

from broker.auth import require_paired_user
from broker.clients import apple_music
from broker.db import db
from broker.models import AppleMusicPlay, AppleMusicToken
from broker.services import device_keys

if TYPE_CHECKING:
    from flask.typing import ResponseReturnValue

bp = Blueprint("apple_music_link", __name__)


@bp.get("/device/apple-music/developer-token", defaults={"device_id": None})
@bp.get("/device/<device_id>/apple-music/developer-token")
@require_paired_user
def developer_token() -> ResponseReturnValue:
    """Return what MusicKit JS is configured with: a developer token and the app's name. 404 if not set up here."""
    if not apple_music.configured():
        return jsonify(error="Apple Music isn't set up on this server"), 404
    return jsonify(developer_token=apple_music.developer_token(), app_name=current_app.config["APPLE_MUSIC_APP_NAME"])


@bp.post("/device/apple-music/connect", defaults={"device_id": None})
@bp.post("/device/<device_id>/apple-music/connect")
@require_paired_user
def connect() -> ResponseReturnValue:
    """Store the Music User Token MusicKit JS got when the listener signed in to Apple."""
    token = request.form.get("music_user_token", "").strip()
    if not token:
        return jsonify(error="No Apple Music token was sent"), 400
    stored = db.session.get(AppleMusicToken, g.device_id) or AppleMusicToken(device_id=g.device_id, seen_ids=[])
    stored.token = token
    db.session.add(stored)
    db.session.commit()
    return redirect(url_for("pages.device_config", device_id=g.device_id))


@bp.post("/device/apple-music/disconnect", defaults={"device_id": None})
@bp.post("/device/<device_id>/apple-music/disconnect")
@require_paired_user
def disconnect() -> ResponseReturnValue:
    """Forget the screen's Apple Music token and the plays it logged."""
    stored = db.session.get(AppleMusicToken, g.device_id)
    if stored is not None:
        db.session.delete(stored)
    db.session.query(AppleMusicPlay).filter_by(device_id=g.device_id).delete()
    db.session.commit()
    # What it last played, and the top lists counted from its plays: the music provider writes them again within a
    # minute for whatever is still linked.
    device_keys.delete(device_keys.music(g.device_id))
    return redirect(url_for("pages.device_config", device_id=g.device_id))
