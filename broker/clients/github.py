"""Talks to GitHub on a device's behalf, with the token its owner gave for the developer panel.

What the panel shows: how many repos the owner has, their open pull requests, the ones waiting for their review,
which repos are failing on their default branch, and their Copilot premium requests this month.
"""

from datetime import UTC, datetime, timedelta

import requests

API_URL = "https://api.github.com"
TIMEOUT = 10
# Only repos pushed to this recently are checked for failing builds: older ones are settled, and each costs a call.
ACTIVE = timedelta(days=30)
MOST_CHECKED = 20
PAGE_SIZE = 100
# What a finished run ends in when it didn't pass.
FAILED = {"failure", "timed_out", "startup_failure"}


def summary(token: str) -> dict:
    """Return the owner's GitHub at a glance, as /api/developer's `github`.

    Raises requests.HTTPError if GitHub refuses the token or fails.
    """
    session = _session(token)
    login = _get(session, "/user")["login"]
    repos = _owned_repos(session)
    return {
        "login": login,
        "repos": len(repos),
        "open_prs": _count(session, f"is:pr is:open author:{login}"),
        "review_requests": _count(session, f"is:pr is:open review-requested:{login}"),
        "failing": [repo["name"] for repo in _recently_pushed(repos) if _failing(session, repo)],
    }


def copilot_premium_requests(token: str, login: str) -> int | None:
    """Return the Copilot premium requests used this month, or None if GitHub won't say.

    It only says for a plan the owner pays for themselves, and only to a token with the Plan permission.
    """
    response = _session(token).get(
        f"{API_URL}/users/{login}/settings/billing/premium_request/usage",
        params={"product": "Copilot"},
        timeout=TIMEOUT,
    )
    if not response.ok:
        return None
    return round(sum(item.get("grossQuantity", 0) for item in response.json().get("usageItems", [])))


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
