"""Talks to GitHub on a device's behalf, with the token its owner gave for the developer panel.

What the panel shows: how many repos the owner has, their open pull requests, the ones waiting for their review,
which repos are failing on their default branch, any release running (or just finished) with its jobs as GitHub's
graph, and their Copilot premium requests this month.
"""

import logging
import re
import time
from datetime import UTC, datetime, timedelta
from http import HTTPStatus

import requests

from broker.clients import workflow_graph

logger = logging.getLogger(__name__)

API_URL = "https://api.github.com"
TIMEOUT = 10
# Only repos pushed to this recently are checked for failing builds: older ones are settled, and each costs a call.
ACTIVE = timedelta(days=30)
MOST_CHECKED = 20
PAGE_SIZE = 100
# What a finished run ends in when it didn't pass.
FAILED = {"failure", "timed_out", "startup_failure"}
# A workflow is a release if its name says so.
RELEASE = re.compile(r"release|deploy", re.IGNORECASE)
# Runs started this recently are looked through for a release: one takes minutes, so this is plenty.
RECENT_RUNS = timedelta(hours=6)
# A finished release stays on the screen this long, so its result is seen. The provider keeps the fast pace while it
# shows (broker/developer_provider.py), so it goes within about BURST_INTERVAL_S of this.
SHOWN_AFTER = timedelta(minutes=1)
# How long to leave GitHub alone after a rate limit that doesn't say how long: GitHub asks for at least a minute.
DEFAULT_BACKOFF_S = 60


def summary(token: str) -> dict:
    """Return the owner's GitHub at a glance, the developer panel's `github`.

    Raises requests.HTTPError if GitHub refuses the token or fails.
    """
    session = _session(token)
    login = _get(session, "/user")["login"]
    repos = _owned_repos(session)
    recent = _recently_pushed(repos)
    return {
        "login": login,
        "repos": len(repos),
        "open_prs": _count(session, f"is:pr is:open author:{login}"),
        "review_requests": _count(session, f"is:pr is:open review-requested:{login}"),
        "failing": [repo["name"] for repo in recent if _failing(session, repo)],
        "releases": _releases(session, recent),
    }


def copilot_premium_requests(token: str, login: str) -> int | None:
    """Return the Copilot premium requests used this month, or None if GitHub won't say.

    It only says for a plan the owner pays for themselves, and only to a token with the Plan permission. Raises
    requests.HTTPError if GitHub is rate-limiting the token (which isn't GitHub not saying).
    """
    response = _session(token).get(
        f"{API_URL}/users/{login}/settings/billing/premium_request/usage",
        params={"product": "Copilot"},
        timeout=TIMEOUT,
    )
    if not response.ok:
        _raise_if_rate_limited(response)
        return None
    return round(sum(item.get("grossQuantity", 0) for item in response.json().get("usageItems", [])))


def rate_limit_wait(error: Exception, now: float | None = None) -> float | None:
    """Return how many seconds GitHub asks to be left alone if `error` is its rate limit; None if it's anything else.

    GitHub answers a rate-limited call with 429 or 403 (a 403 is also a token without the permission, which isn't
    one): with Retry-After (seconds) for a secondary limit, or X-RateLimit-Remaining 0 and X-RateLimit-Reset (epoch
    seconds) for the 5,000 an hour. Without either, at least a minute. `now` is the epoch time (for the tests).
    """
    response = error.response if isinstance(error, requests.HTTPError) else None
    if response is None or response.status_code not in {HTTPStatus.FORBIDDEN, HTTPStatus.TOO_MANY_REQUESTS}:
        return None
    headers = response.headers or {}
    if (retry := str(headers.get("Retry-After", ""))).isdigit():
        return max(int(retry), 1)
    reset = str(headers.get("X-RateLimit-Reset", ""))
    if headers.get("X-RateLimit-Remaining") == "0" and reset.isdigit():
        return max(int(reset) - (time.time() if now is None else now), 1)
    if response.status_code == HTTPStatus.TOO_MANY_REQUESTS or "rate limit" in (response.text or "").lower():
        return DEFAULT_BACKOFF_S
    return None


def _raise_if_rate_limited(response: requests.Response) -> None:
    """Raise requests.HTTPError if `response` is GitHub's rate limit, which the callers that skip a failure mustn't."""
    try:
        response.raise_for_status()
    except requests.HTTPError as e:
        if rate_limit_wait(e) is not None:
            raise


def _session(token: str) -> requests.Session:
    session = requests.Session()
    session.headers.update(
        {
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
    )
    return session


def _get(session: requests.Session, path: str, **params: str | int) -> dict:
    response = session.get(f"{API_URL}{path}", params=params, timeout=TIMEOUT)
    response.raise_for_status()
    return response.json()


def _owned_repos(session: requests.Session) -> list[dict]:
    """Return every repo the owner owns, archived ones left out, most recently pushed first."""
    repos: list[dict] = []
    page = 1
    while True:
        response = session.get(
            f"{API_URL}/user/repos",
            params={"affiliation": "owner", "sort": "pushed", "per_page": PAGE_SIZE, "page": page},
            timeout=TIMEOUT,
        )
        response.raise_for_status()
        batch = response.json()
        repos += [repo for repo in batch if not repo.get("archived")]
        if len(batch) < PAGE_SIZE:
            return repos
        page += 1


def _count(session: requests.Session, query: str) -> int:
    return _get(session, "/search/issues", q=query, per_page=1)["total_count"]


def _recently_pushed(repos: list[dict]) -> list[dict]:
    since = datetime.now(UTC) - ACTIVE
    recent = [repo for repo in repos if repo.get("pushed_at") and datetime.fromisoformat(repo["pushed_at"]) >= since]
    return recent[:MOST_CHECKED]


def _failing(session: requests.Session, repo: dict) -> bool:
    """Whether any workflow's latest finished run on the repo's default branch failed."""
    runs = _get(
        session,
        f"/repos/{repo['full_name']}/actions/runs",
        branch=repo["default_branch"],
        status="completed",
        exclude_pull_requests="true",
        per_page=20,
    )["workflow_runs"]
    latest: dict[int, str] = {}
    for run in runs:  # newest first
        latest.setdefault(run["workflow_id"], run["conclusion"])
    return any(conclusion in FAILED for conclusion in latest.values())


def _releases(session: requests.Session, repos: list[dict]) -> list[dict]:
    """Return each repo's release that's running or finished in the last SHOWN_AFTER, the latest started first.

    A repo whose release can't be read is left out, so the rest of the panel still shows; but a rate limit is raised
    (requests.HTTPError), as every call after it would fail too and the panel would lose its releases for nothing.
    """
    now = datetime.now(UTC)
    found = []
    for repo in repos:
        try:
            release = _release(session, repo, now)
        except requests.RequestException as e:
            if rate_limit_wait(e) is not None:
                raise
            logger.warning("Couldn't read %s's release: %s", repo["full_name"], e)
            continue
        if release is not None:
            found.append(release)
    return sorted(found, key=lambda release: release["started"], reverse=True)


def _release(session: requests.Session, repo: dict, now: datetime) -> dict | None:
    """Return the repo's latest release run with its jobs as graph nodes; None if none, or it finished a while ago."""
    runs = _get(
        session,
        f"/repos/{repo['full_name']}/actions/runs",
        created=f">={now - RECENT_RUNS:%Y-%m-%dT%H:%M:%SZ}",
        per_page=30,
    )["workflow_runs"]
    run = next((run for run in runs if RELEASE.search(run["name"])), None)  # newest first
    if run is None:
        return None
    finished = run["status"] == "completed"
    if finished and datetime.fromisoformat(run["updated_at"]) < now - SHOWN_AFTER:
        return None
    jobs = _get(session, f"/repos/{repo['full_name']}/actions/runs/{run['id']}/jobs", per_page=100)["jobs"]
    return {
        "repo": repo["name"],
        "title": _title(run, repo),
        "state": workflow_graph.run_state(run),
        "started": run["run_started_at"],
        "finished": run["updated_at"] if finished else None,
        "jobs": workflow_graph.nodes(_workflow_jobs(session, repo, run), jobs),
    }


def _title(run: dict, repo: dict) -> str:
    """Return the tag a release was pushed for, else its run number (one started by hand runs on the default branch)."""
    if run["event"] == "push" and run["head_branch"] != repo["default_branch"]:
        return run["head_branch"]
    return f"#{run['run_number']}"


def _workflow_jobs(session: requests.Session, repo: dict, run: dict) -> list[workflow_graph.WorkflowJob]:
    """Return the jobs of the run's workflow file, as at the run's commit; none if it can't be read.

    Without them the graph still shows the run's jobs, but not the lines between them. A rate limit is raised
    (requests.HTTPError), as in _releases.
    """
    path = run["path"].split("@")[0]
    try:
        response = session.get(
            f"{API_URL}/repos/{repo['full_name']}/contents/{path}",
            params={"ref": run["head_sha"]},
            headers={"Accept": "application/vnd.github.raw+json"},
            timeout=TIMEOUT,
        )
        response.raise_for_status()
        return workflow_graph.workflow_jobs(response.text)
    except (requests.RequestException, ValueError) as e:
        if rate_limit_wait(e) is not None:
            raise
        logger.warning("Couldn't read %s's %s: %s", repo["full_name"], path, e)
        return []
