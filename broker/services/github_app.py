"""The GitHub dashboard's GitHub App: signing its user in, keeping their token fresh, and the webhook's change feed.

The App acts as the signed-in user with user access tokens, so deploy approvals still come from a required reviewer;
an installation token would act as the App, which no environment lists. GitHub expires the access token after 8 hours
and the refresh token after 6 months, and a refresh token works once: renewing takes a Redis lock, so two workers
never spend the same one.

The webhook doesn't call GitHub: it appends what changed to a Redis stream, which the dashboard reads every few
seconds and answers by re-reading only those items.
"""

import hashlib
import hmac
import logging
import time
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING
from urllib.parse import urlencode

import requests
from flask import current_app

from broker.clients import github
from broker.db import db
from broker.models import GitHubAppToken

if TYPE_CHECKING:
    from redis import Redis

logger = logging.getLogger(__name__)

AUTHORIZE_URL = "https://github.com/login/oauth/authorize"
TOKEN_URL = "https://github.com/login/oauth/access_token"  # noqa: S105 -- a URL, not a secret
TIMEOUT = 10
# Renew this long before the access token expires, so no request starts with one about to lapse.
RENEW_AHEAD = timedelta(minutes=5)
RENEW_LOCK = "v1:github:app-token-renewal"
RENEW_LOCK_S = 30
# How long a worker waits for another's renewal before giving up on this request.
RENEW_WAIT_S = 30
CHANGES = "v1:github:changes"
# Enough for a few minutes of a busy CI run; a dashboard further behind is told to read everything.
CHANGES_KEPT = 2000


class NotConnectedError(github.DashboardError):
    """The App is set up but its user hasn't signed in, or GitHub no longer accepts the sign-in."""

    def __init__(self) -> None:
        """Tell the user what to do about it."""
        super().__init__("Connect GitHub to use the dashboard.")


def configured() -> bool:
    """Whether the GitHub App's client is set up for signing in."""
    config = current_app.config
    return bool(config["GITHUB_APP_CLIENT_ID"] and config["GITHUB_APP_CLIENT_SECRET"])


def authorize_url(state: str) -> str:
    """Where to send the user to sign in; GitHub returns them to the App's callback URL."""
    return f"{AUTHORIZE_URL}?{urlencode({'client_id': current_app.config['GITHUB_APP_CLIENT_ID'], 'state': state})}"


def connect(user_id: str, code: str) -> None:
    """Swap the callback's code for tokens and keep them for `user_id`."""
    _save(user_id, _token_request({"code": code}))


def token(user_id: str) -> str:
    """Return a current access token for `user_id`, renewing it first when it's about to expire."""
    row = db.session.get(GitHubAppToken, user_id)
    if row is None:
        raise NotConnectedError
    if datetime.now(UTC) < row.expires_at - RENEW_AHEAD:
        return row.access_token
    redis: Redis = current_app.extensions["redis"]
    deadline = time.monotonic() + RENEW_WAIT_S
    while not redis.set(RENEW_LOCK, "1", nx=True, ex=RENEW_LOCK_S):
        if time.monotonic() > deadline:
            msg = "Timed out waiting for another worker to renew the GitHub token."
            raise TimeoutError(msg)
        time.sleep(0.2)
    try:
        # Another worker may have renewed it while this one waited.
        db.session.expire_all()
        row = db.session.get(GitHubAppToken, user_id)
        if row is None:
            raise NotConnectedError
        if datetime.now(UTC) < row.expires_at - RENEW_AHEAD:
            return row.access_token
        if datetime.now(UTC) >= row.refresh_expires_at:
            disconnect(user_id)
            raise NotConnectedError
        try:
            fresh = _token_request({"grant_type": "refresh_token", "refresh_token": row.refresh_token})
        except NotConnectedError:
            disconnect(user_id)
            raise
        return _save(user_id, fresh).access_token
    finally:
        redis.delete(RENEW_LOCK)


def disconnect(user_id: str) -> None:
    """Forget `user_id`'s tokens."""
    db.session.query(GitHubAppToken).filter_by(user_id=user_id).delete()
    db.session.commit()


def connected(user_id: str) -> bool:
    """Whether `user_id` has signed in through the App."""
    return db.session.get(GitHubAppToken, user_id) is not None


def _token_request(fields: dict[str, str]) -> dict:
    config = current_app.config
    response = requests.post(
        TOKEN_URL,
        data={"client_id": config["GITHUB_APP_CLIENT_ID"], "client_secret": config["GITHUB_APP_CLIENT_SECRET"]}
        | fields,
        headers={"Accept": "application/json"},
        timeout=TIMEOUT,
    )
    response.raise_for_status()
    data = response.json()
    # GitHub answers a refused code or refresh token with 200 and an error.
    if "error" in data or not data.get("refresh_token"):
        logger.warning("GitHub refused the App token request: %s", data.get("error", "no refresh token"))
        raise NotConnectedError
    return data


def _save(user_id: str, data: dict) -> GitHubAppToken:
    now = datetime.now(UTC)
    row = db.session.get(GitHubAppToken, user_id) or GitHubAppToken(user_id=user_id)
    row.access_token = data["access_token"]
    row.refresh_token = data["refresh_token"]
    row.expires_at = now + timedelta(seconds=int(data["expires_in"]))
    row.refresh_expires_at = now + timedelta(seconds=int(data["refresh_token_expires_in"]))
    db.session.add(row)
    db.session.commit()
    return row


def valid_signature(body: bytes, signature: str) -> bool:
    """Whether `signature` (X-Hub-Signature-256) is the webhook secret's HMAC of `body`."""
    secret = current_app.config["GITHUB_APP_WEBHOOK_SECRET"]
    expected = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return bool(secret) and hmac.compare_digest(expected.encode(), signature.encode())


def changes_in(event: str, payload: dict) -> list[dict[str, str]]:
    """List what a webhook delivery changed, as items the dashboard can re-read: a PR, a run, or a whole repository."""
    if event.startswith("installation"):
        return [{"kind": "repositories"}]
    repo = (payload.get("repository") or {}).get("full_name")
    if not isinstance(repo, str):
        return []
    changed: list[tuple[str, object]] = []
    if event in {"pull_request", "pull_request_review"}:
        changed = [("pull", payload["pull_request"]["number"])]
    elif event in {"check_run", "check_suite"}:
        changed = [("pull", pr["number"]) for pr in payload[event].get("pull_requests") or []] or [("repo", None)]
    # Only release runs are on the dashboard; CI runs reach it through their PR's checks.
    elif event == "workflow_run" and github.is_release_name(payload["workflow_run"]["name"]):
        changed = [("run", payload["workflow_run"]["id"])]
    elif event == "workflow_job" and github.is_release_name(payload["workflow_job"].get("workflow_name")):
        changed = [("run", payload["workflow_job"]["run_id"])]
    elif event in {"status", "deployment_status", "push"}:
        changed = [("repo", None)]
    return [{"repo": repo, "kind": kind} | ({"id": str(item)} if item is not None else {}) for kind, item in changed]


def record(changes: list[dict[str, str]]) -> None:
    """Append changes to the feed the dashboard reads."""
    redis: Redis = current_app.extensions["redis"]
    for change in changes:
        redis.xadd(CHANGES, change, maxlen=CHANGES_KEPT, approximate=True)


def changes_after(after: str | None) -> dict:
    """Return changes since the feed entry `after`, the newest entry's ID, and whether some were lost."""
    redis: Redis = current_app.extensions["redis"]
    newest = redis.xrevrange(CHANGES, count=1)
    last = newest[0][0] if newest else "0-0"
    if after is None:
        return {"last": last, "changes": [], "reset": False}
    oldest = redis.xrange(CHANGES, count=1)
    # Trimmed past `after`, or emptied (a Redis restart) so that `after` is ahead of everything.
    reset = (bool(oldest) and _stream_id(oldest[0][0]) > _stream_id(after) and after != "0-0") or (
        _stream_id(after) > _stream_id(last)
    )
    entries = redis.xrange(CHANGES, min=f"({after}", max="+", count=CHANGES_KEPT) if not reset else []
    return {"last": last, "changes": [fields for _, fields in entries], "reset": reset}


def _stream_id(value: str) -> tuple[int, int]:
    millis, _, sequence = value.partition("-")
    return int(millis), int(sequence or 0)
