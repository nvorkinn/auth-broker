"""GitHub's webhook for the dashboard's App: outside the login, so its signature is the only check."""

import json
from typing import TYPE_CHECKING

from flask import Blueprint, current_app, jsonify, request

from broker.services import github_app

if TYPE_CHECKING:
    from flask.typing import ResponseReturnValue

bp = Blueprint("github_webhook", __name__)


@bp.post("/api/github/webhook")
def webhook() -> ResponseReturnValue:
    """Note what a signed delivery changed for the dashboard to re-read; anything unsigned is refused."""
    if not current_app.config["GITHUB_APP_WEBHOOK_SECRET"]:
        return jsonify(error="Not found."), 404
    body = request.get_data(cache=False)
    if not github_app.valid_signature(body, request.headers.get("X-Hub-Signature-256", "")):
        return jsonify(error="Invalid signature."), 401
    try:
        payload = json.loads(body)
        changes = github_app.changes_in(request.headers.get("X-GitHub-Event", ""), payload)
    except ValueError, KeyError, TypeError, AttributeError:
        current_app.logger.warning("GitHub webhook delivery had an unexpected shape")
        return jsonify(error="Unreadable delivery."), 400
    github_app.record(changes)
    return "", 204
