"""Personal GitHub monitoring, protected by Authentik identity and a release CSRF token."""

import functools
import hmac
import json
import re
import secrets
from typing import TYPE_CHECKING

import requests
from flask import Blueprint, Response, current_app, jsonify, render_template, request, session

from broker.auth import View, current_user, require_user
from broker.clients import github

if TYPE_CHECKING:
    from collections.abc import Callable

    from flask.typing import ResponseReturnValue

bp = Blueprint("github_dashboard", __name__, url_prefix="/github")
REPO_PART = re.compile(r"^[A-Za-z0-9_.-]+$")


def dashboard_view(view: Callable[..., ResponseReturnValue]) -> View:
    """Fail closed unless the dashboard is configured and this is its one allowed Authentik user."""

    @functools.wraps(view)
    @require_user
    def wrapped(**kwargs: str) -> ResponseReturnValue:
        config = current_app.config
        if not config["GITHUB_DASHBOARD_TOKEN"] or not config["GITHUB_DASHBOARD_USER_ID"]:
            return jsonify(error="The GitHub dashboard is not configured."), 503
        if current_user() != config["GITHUB_DASHBOARD_USER_ID"]:
            return jsonify(error="This dashboard is private."), 403
        if any(not REPO_PART.fullmatch(value) or value in {".", ".."} for value in kwargs.values()):
            return jsonify(error="Invalid repository name."), 400
        try:
            return view(**kwargs)
        except requests.RequestException as error:
            return _upstream_error(error)
        except github.DashboardError as error:
            current_app.logger.warning("GitHub dashboard operation refused: %s", error)
            return jsonify(error=str(error)), 409

    return wrapped


@bp.errorhandler(json.JSONDecodeError)
def unreadable_json(error: json.JSONDecodeError) -> ResponseReturnValue:
    """Report malformed upstream or cache JSON without presenting stale data as current."""
    current_app.logger.warning("GitHub dashboard received malformed JSON: %s", error)
    return jsonify(error="GitHub returned an unreadable response."), 502


def _upstream_error(error: requests.RequestException) -> ResponseReturnValue:
    status = error.response.status_code if error.response is not None else None
    current_app.logger.warning("GitHub dashboard request failed (upstream status %s)", status)
    wait = github.rate_limit_wait(error)
    if wait is not None:
        return jsonify(error="GitHub rate limit reached. Please wait."), 429, {"Retry-After": str(int(wait))}
    message = "GitHub request failed. Check the PAT's access and permissions."
    if request.method == "POST":
        message += " Publication may have succeeded; check GitHub releases before trying again."
    return jsonify(error=message), 502


@bp.after_request
def private_response(response: Response) -> Response:
    """Keep private repository data and CSRF tokens out of shared caches."""
    response.headers["Cache-Control"] = "no-store"
    return response


@bp.get("")
@bp.get("/")
@dashboard_view
def index() -> ResponseReturnValue:
    """Serve the dashboard without placing the PAT in the browser."""
    if "github_dashboard_csrf" not in session:
        session["github_dashboard_csrf"] = secrets.token_urlsafe(32)
    return render_template("github_dashboard.html", csrf=session["github_dashboard_csrf"])


@bp.get("/repositories")
@dashboard_view
def repositories() -> ResponseReturnValue:
    """Discover every owned repository accessible to the PAT."""
    api = github.Dashboard(current_app.config["GITHUB_DASHBOARD_TOKEN"], current_app.extensions["redis"])
    try:
        return jsonify(repositories=api.repositories())
    finally:
        api.close()


@bp.get("/repositories/<owner>/<name>")
@dashboard_view
def project(owner: str, name: str) -> ResponseReturnValue:
    """Read a project's PRs and unfinished Actions runs."""
    api = github.Dashboard(current_app.config["GITHUB_DASHBOARD_TOKEN"], current_app.extensions["redis"])
    try:
        return jsonify(api.project(api.owned_repository(owner, name)))
    finally:
        api.close()


@bp.post("/repositories/<owner>/<name>/release")
@dashboard_view
def release(owner: str, name: str) -> ResponseReturnValue:
    """Publish immediately, but only for a same-session dashboard action."""
    expected = session.get("github_dashboard_csrf", "")
    received = request.headers.get("X-CSRF-Token", "")
    if not expected or not hmac.compare_digest(expected.encode(), received.encode()):
        return jsonify(error="Invalid release CSRF token. Reload the dashboard."), 403
    api = github.Dashboard(current_app.config["GITHUB_DASHBOARD_TOKEN"], current_app.extensions["redis"])
    try:
        result = api.publish_release(api.owned_repository(owner, name))
        return jsonify(tag=result["tag_name"], url=result["html_url"]), 201
    finally:
        api.close()
