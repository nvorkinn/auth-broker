"""The personal GitHub dashboard, for one Authentik user, its actions guarded by a CSRF token."""

import functools
import hmac
import json
import re
import secrets
from typing import TYPE_CHECKING
from urllib.parse import urlencode

import requests
from flask import Blueprint, Response, current_app, jsonify, redirect, render_template, request, session

from broker.auth import View, require_user_id
from broker.clients import github
from broker.services import github_app

if TYPE_CHECKING:
    from collections.abc import Callable

    from flask.typing import ResponseReturnValue

bp = Blueprint("github_dashboard", __name__, url_prefix="/github")
REPO_PART = re.compile(r"^[A-Za-z0-9_.-]+$")
# What a project read may ask for: both halves, or just the one a section's Refresh button needs.
SECTIONS = {"all", "pulls", "workflows"}
STREAM_ID = re.compile(r"^\d{1,20}-\d{1,20}$")


def _serve(view: Callable[..., ResponseReturnValue], user_id: str, kwargs: dict[str, str]) -> ResponseReturnValue:
    """Run a dashboard view, unless the dashboard isn't set up or this isn't its one allowed user."""
    config = current_app.config
    if not github_app.configured() or not config["GITHUB_DASHBOARD_USER_ID"]:
        return jsonify(error="The GitHub dashboard is not configured."), 503
    if user_id != config["GITHUB_DASHBOARD_USER_ID"]:
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


def dashboard_view(view: Callable[..., ResponseReturnValue]) -> View:
    """Fail closed unless the dashboard is configured and this is its one allowed Authentik user."""

    @functools.wraps(view)
    @require_user_id
    def wrapped(*, user_id: str, **kwargs: str) -> ResponseReturnValue:
        return _serve(view, user_id, kwargs)

    return wrapped


def dashboard_user_view(view: Callable[..., ResponseReturnValue]) -> View:
    """Guard a dashboard view like dashboard_view, and pass it the user's id as `user_id`."""

    @functools.wraps(view)
    @require_user_id
    def wrapped(*, user_id: str, **kwargs: str) -> ResponseReturnValue:
        return _serve(functools.partial(view, user_id=user_id), user_id, kwargs)

    return wrapped


def _dashboard(user_id: str) -> github.Dashboard:
    """Open the GitHub client as the user, through the GitHub App they've connected."""
    return github.Dashboard(github_app.token(user_id), current_app.extensions["redis"])


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
    message = "GitHub request failed. Check the token's access and permissions."
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
@dashboard_user_view
def index(user_id: str) -> ResponseReturnValue:
    """Serve the dashboard without placing the user's GitHub token in the browser."""
    if "github_dashboard_csrf" not in session:
        session["github_dashboard_csrf"] = secrets.token_urlsafe(32)
    return render_template(
        "github_dashboard.html",
        csrf=session["github_dashboard_csrf"],
        connect=not github_app.connected(user_id),
        webhooks=bool(current_app.config["GITHUB_APP_WEBHOOK_SECRET"]),
    )


@bp.get("/connect")
@dashboard_view
def connect() -> ResponseReturnValue:
    """Send the user to sign in to the GitHub App."""
    state = secrets.token_urlsafe(32)
    session["github_app_state"] = state
    return redirect(github_app.authorize_url(state))


@bp.get("/callback")
@dashboard_user_view
def callback(user_id: str) -> ResponseReturnValue:
    """Finish the sign-in GitHub sent the user back from, if this session started it."""
    expected = session.pop("github_app_state", "")
    received = request.args.get("state", "")
    if not expected or not hmac.compare_digest(expected.encode(), received.encode()):
        return jsonify(error="This sign-in wasn't started here. Press Connect GitHub again."), 400
    code = request.args.get("code", "")
    if not code:
        return jsonify(error="GitHub sign-in was cancelled."), 400
    github_app.connect(user_id, code)
    return redirect("/github")


@bp.get("/changes")
@dashboard_view
def changes() -> ResponseReturnValue:
    """Return what the webhook saw change since the feed entry `after`, read from Redis without calling GitHub."""
    after = request.args.get("after")
    if after is not None and not STREAM_ID.fullmatch(after):
        return jsonify(error="Invalid change ID."), 400
    return jsonify(github_app.changes_after(after))


@bp.get("/repositories")
@dashboard_user_view
def repositories(user_id: str) -> ResponseReturnValue:
    """Discover every owned repository the App can reach."""
    api = _dashboard(user_id)
    try:
        return jsonify(repositories=api.repositories())
    finally:
        api.close()


@bp.get("/repositories/<owner>/<name>")
@dashboard_user_view
def project(owner: str, name: str, user_id: str) -> ResponseReturnValue:
    """Read a project's PRs and release runs, or only one of the two."""
    section = request.args.get("section", "all")
    if section not in SECTIONS:
        return jsonify(error="Invalid section."), 400
    api = _dashboard(user_id)
    try:
        return jsonify(api.project(api.owned_repository(owner, name), section))
    finally:
        api.close()


@bp.get("/repositories/<owner>/<name>/runs/<run_id>")
@dashboard_user_view
def run(owner: str, name: str, run_id: str, user_id: str) -> ResponseReturnValue:
    """Read one release run's current state, for quick polling and the Refresh button on a running workflow."""
    if not run_id.isascii() or not run_id.isdecimal() or int(run_id) <= 0:
        return jsonify(error="Invalid workflow run ID."), 400
    api = _dashboard(user_id)
    try:
        workflow = api.release_run(api.owned_repository(owner, name), int(run_id))
    finally:
        api.close()
    if workflow is None:
        return jsonify(error="This release workflow run no longer exists."), 404
    return jsonify(workflow=workflow)


@bp.get("/repositories/<owner>/<name>/pulls/<number>")
@dashboard_user_view
def pull(owner: str, name: str, number: str, user_id: str) -> ResponseReturnValue:
    """Read one open PR's checks and mergeability, for quick polling while it is busy."""
    if not number.isascii() or not number.isdecimal() or int(number) <= 0:
        return jsonify(error="Invalid pull request number."), 400
    api = _dashboard(user_id)
    try:
        repo = api.owned_repository(owner, name)
        found = api.pull(f"/repos/{repo['full_name']}", int(number))
    finally:
        api.close()
    if found is None:
        return jsonify(error="This pull request is no longer open."), 404
    return jsonify(pull=found)


@bp.get("/repositories/<owner>/<name>/pulls/<number>/files")
@dashboard_user_view
def pull_files(owner: str, name: str, number: str, user_id: str) -> ResponseReturnValue:
    """List a PR's changed files and patches, read when its Files changed list is opened."""
    if not number.isascii() or not number.isdecimal() or int(number) <= 0:
        return jsonify(error="Invalid pull request number."), 400
    api = _dashboard(user_id)
    try:
        repo = api.owned_repository(owner, name)
        return jsonify(api.pull_files(f"/repos/{repo['full_name']}", int(number)))
    finally:
        api.close()


@bp.post("/repositories/<owner>/<name>/pulls/<number>/auto-merge")
@dashboard_user_view
def auto_merge(owner: str, name: str, number: str, user_id: str) -> ResponseReturnValue:
    """Turn a PR's auto-merge on or off, for a request carrying the session's CSRF token."""
    if not _valid_csrf():
        return jsonify(error="Invalid action CSRF token. Reload the dashboard."), 403
    payload = request.get_json(silent=True)
    enabled = payload.get("enabled") if isinstance(payload, dict) else None
    if not number.isascii() or not number.isdecimal() or int(number) <= 0:
        return jsonify(error="Invalid pull request number."), 400
    if type(enabled) is not bool:
        return jsonify(error="enabled must be true or false."), 400
    api = _dashboard(user_id)
    try:
        return jsonify(api.set_auto_merge(api.owned_repository(owner, name), int(number), enabled=enabled))
    finally:
        api.close()


@bp.post("/repositories/<owner>/<name>/release")
@dashboard_user_view
def release(owner: str, name: str, user_id: str) -> ResponseReturnValue:
    """Publish the next minor release immediately, for a request carrying the session's CSRF token."""
    if not _valid_csrf():
        return jsonify(error="Invalid action CSRF token. Reload the dashboard."), 403
    api = _dashboard(user_id)
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
@dashboard_user_view
def approve(owner: str, name: str, run_id: str, user_id: str) -> ResponseReturnValue:
    """Release one pending environment without bypassing GitHub's reviewer restrictions."""
    if not _valid_csrf():
        return jsonify(error="Invalid action CSRF token. Reload the dashboard."), 403
    payload = request.get_json(silent=True)
    environment_id = payload.get("environment_id") if isinstance(payload, dict) else None
    if not run_id.isascii() or not run_id.isdecimal() or int(run_id) <= 0:
        return jsonify(error="Invalid workflow run ID."), 400
    if type(environment_id) is not int or environment_id <= 0:
        return jsonify(error="A positive environment_id is required."), 400
    api = _dashboard(user_id)
    try:
        api.approve_deployment(api.owned_repository(owner, name), int(run_id), environment_id)
        return jsonify(approved=True)
    finally:
        api.close()
