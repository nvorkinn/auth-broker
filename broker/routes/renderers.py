"""What a renderer the orchestrator started calls once to take its place. Unlike /api/devices/register it
isn't throttled: the claim token, issued per assignment and single-use, is what protects it."""

from flask import Blueprint, jsonify, request

from ..auth import hash_secret
from ..services import registration

bp = Blueprint("renderers", __name__, url_prefix="/api/renderers")


@bp.post("/claim")
def claim():
    """{"secret": ..., "claim_token": ...}: attaches the renderer's secret to its assignment's device and
    spends the token (201 {device_id}). Safe to repeat with the same secret, e.g. on every start (200).
    An unknown or spent token is a 403."""
    body = request.get_json(silent=True) or {}
    secret, claim_token = body.get("secret", ""), body.get("claim_token")
    if not isinstance(secret, str) or len(secret) < 16:
        return jsonify(error="secret must be a random string of at least 16 characters"), 400
    if not isinstance(claim_token, str) or not claim_token:
        return jsonify(error="claim_token must be a string"), 400

    found = registration.claim(hash_secret(secret), claim_token)
    if found is None:
        return jsonify(error="unknown or spent claim token"), 403
    if found.role_conflict:
        return jsonify(error="that secret is already registered as a screen"), 409
    return jsonify(device_id=found.device_id), 201 if found.created else 200
