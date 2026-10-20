"""The personal GitHub dashboard, for one Authentik user, its actions guarded by a CSRF token."""

import functools
import hmac
import json
import re
import secrets
from typing import TYPE_CHECKING
from urllib.parse import urlencode

import requests
from flask import Blueprint, Response, current_app, jsonify, render_template, request, session

from broker.auth import View, current_user, require_user
from broker.clients import github

if TYPE_CHECKING:
    from collections.abc import Callable

    from flask.typing import ResponseReturnValue

bp = Blueprint("github_dashboard", __name__, url_prefix="/github")
REPO_PART = re.compile(r"^[A-Za-z0-9_.-]+$")
# Runs the page showed last time, rechecked by ID; each costs a GitHub call when the status lists miss it.
MAX_KNOWN_RUNS = 20
# What a project read may ask for: both halves, or just the one a section's Refresh button needs.
SECTIONS = {"all", "pulls", "workflows"}


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
            return jsonify(error=error.public_message), 409

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
        message += " The action may have succeeded; check GitHub Actions or releases before trying again."
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
    """Read a project's PRs and unfinished Actions runs, or only one of the two."""
    known = request.args.get("runs", "").split(",") if request.args.get("runs") else []
    if len(known) > MAX_KNOWN_RUNS or not all(run.isascii() and run.isdecimal() and int(run) > 0 for run in known):
        return jsonify(error="Invalid workflow run IDs."), 400
    section = request.args.get("section", "all")
    if section not in SECTIONS:
        return jsonify(error="Invalid section."), 400
    api = github.Dashboard(current_app.config["GITHUB_DASHBOARD_TOKEN"], current_app.extensions["redis"])
    try:
        return jsonify(api.project(api.owned_repository(owner, name), [int(run) for run in known], section))
    finally:
        api.close()


@bp.post("/repositories/<owner>/<name>/release")
@dashboard_view
def release(owner: str, name: str) -> ResponseReturnValue:
    """Publish the next minor release immediately, for a request carrying the session's CSRF token."""
    if not _valid_csrf():
        return jsonify(error="Invalid action CSRF token. Reload the dashboard."), 403
    api = github.Dashboard(current_app.config["GITHUB_DASHBOARD_TOKEN"], current_app.extensions["redis"])
    try:
        repo = api.owned_repository(owner, name)
        result = api.publish_release(repo)
        query = urlencode({"query": f"branch:{result['tag_name']}"})
        return jsonify(tag=result["tag_name"], url=f"{repo['html_url']}/actions?{query}"), 201
    finally:
        api.close()


def _valid_csrf() -> bool:
    expected = session.get("github_dashboard_csrf", "")
    received = request.headers.get("X-CSRF-Token", "")
    return bool(expected) and hmac.compare_digest(expected.encode(), received.encode())


@bp.post("/repositories/<owner>/<name>/runs/<run_id>/approve")
@dashboard_view
def approve(owner: str, name: str, run_id: str) -> ResponseReturnValue:
    """Release one pending environment without bypassing GitHub's reviewer restrictions."""
    if not _valid_csrf():
        return jsonify(error="Invalid action CSRF token. Reload the dashboard."), 403
    payload = request.get_json(silent=True)
    environment_id = payload.get("environment_id") if isinstance(payload, dict) else None
    if not run_id.isascii() or not run_id.isdecimal() or int(run_id) <= 0:
        return jsonify(error="Invalid workflow run ID."), 400
    if type(environment_id) is not int or environment_id <= 0:
        return jsonify(error="A positive environment_id is required."), 400
    api = github.Dashboard(current_app.config["GITHUB_DASHBOARD_TOKEN"], current_app.extensions["redis"])
    try:
        api.approve_deployment(api.owned_repository(owner, name), int(run_id), environment_id)
        return jsonify(approved=True)
    finally:
        api.close()
