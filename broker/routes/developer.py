"""The developer panel's data for the Pi.

The broker holds the device's GitHub token and makes the calls itself, so a device only ever presents its own secret,
never a GitHub token.
"""

import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import requests
from flask import Blueprint, jsonify

from broker.auth import require_device_auth
from broker.db import db
from broker.models import DeviceConfig, GitHubToken
from broker.services import developer as developer_data

if TYPE_CHECKING:
    from flask.typing import ResponseReturnValue

logger = logging.getLogger(__name__)

bp = Blueprint("developer", __name__, url_prefix="/api/developer")


@bp.get("")
@require_device_auth(roles={"renderer"})
def developer(device_id: str) -> ResponseReturnValue:
    """Return the owner's GitHub at a glance and their AI plans; `github` is null without a token.

    502 if GitHub can't be reached or refuses the token, so the screen keeps what it last showed.
    """
    config = db.session.get(DeviceConfig, device_id)
    if config is None:
        return jsonify(error="not found"), 404
    if not config.developer_enabled:
        return jsonify(github=None, plans=[])

    token = db.session.get(GitHubToken, device_id)
    try:
        return jsonify(developer_data.snapshot(token and token.token, datetime.now(UTC).date()))
    except requests.RequestException as e:
        logger.warning("GitHub for device %s: %s", device_id, e)
        return jsonify(error="GitHub could not be reached"), 502
