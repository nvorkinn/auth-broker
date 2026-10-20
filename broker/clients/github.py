"""Talks to GitHub: for the developer panel with a device owner's token, and for the /github dashboard (Dashboard).

The panel shows how many repos the owner has, their open pull requests, the ones waiting for their review, which repos
are failing on their default branch, any release running (or just finished) with its jobs as GitHub's graph, and their
Copilot premium requests this month.
"""

import hashlib
import json
import logging
import re
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from http import HTTPStatus
from typing import TYPE_CHECKING

import requests
from redis import RedisError

from broker.clients import workflow_graph

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable

    from redis import Redis

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
# A finished release stays on the screen this long, so its result is seen. The developer provider keeps the fast pace
# while it shows, so it goes within about its BURST_INTERVAL_S of this.
SHOWN_AFTER = timedelta(minutes=1)
# How long to leave GitHub alone after a rate limit that doesn't say how long: GitHub asks for at least a minute.
DEFAULT_BACKOFF_S = 60
# Most GitHub calls one dashboard request makes at once; the session's connection pool is sized to match.
MAX_PARALLEL = 8
POOL_SIZE = 16


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


def _parallel[T](calls: Iterable[Callable[[], T]]) -> list[T]:
    """Run independent GitHub calls at once, returning their results in order and raising the first failure."""
    calls = list(calls)
    if len(calls) <= 1:
        return [call() for call in calls]
    with ThreadPoolExecutor(max_workers=min(len(calls), MAX_PARALLEL)) as pool:
        return [future.result() for future in [pool.submit(call) for call in calls]]


def _session(token: str) -> requests.Session:
    session = requests.Session()
    for prefix in ("https://", "http://"):
        session.mount(prefix, requests.adapters.HTTPAdapter(pool_maxsize=POOL_SIZE))
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


def _is_release(run: dict) -> bool:
    """Tell whether a workflow, or a run of one, is a release the dashboard draws."""
    return "release" in (run["name"] or "").casefold()


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


class DashboardError(ValueError):
    """A dashboard operation that cannot safely proceed."""

    def __init__(self, public_message: str) -> None:
        """Keep a deliberately user-facing message separate from exception diagnostics."""
        super().__init__(public_message)
        self.public_message = public_message


class Dashboard:
    """The personal dashboard's GitHub operations, all performed with its server-side PAT."""

    def __init__(self, token: str, cache: Redis | None = None) -> None:
        """Open a session for one dashboard request."""
        self.session = _session(token)
        self.cache = cache
        self.cache_scope = hashlib.sha256(token.encode()).hexdigest()

    def close(self) -> None:
        """Release the request's HTTP connections."""
        self.session.close()

    def read(self, path: str, *, raw: bool = False, **params: str | int) -> str:
        """Return the text at `path`, revalidating a cached ETag: GitHub doesn't count authenticated 304s."""
        identity = json.dumps([self.cache_scope, path, params, raw], sort_keys=True)
        key = f"github-dashboard:{hashlib.sha256(identity.encode()).hexdigest()}"
        cached = None
        if self.cache is not None:
            try:
                if value := self.cache.get(key):
                    cached = json.loads(value)
            except RedisError:
                logger.warning("GitHub dashboard ETag cache read failed", exc_info=True)
        headers = {"Accept": "application/vnd.github.raw+json" if raw else "application/vnd.github+json"}
        if cached:
            headers["If-None-Match"] = cached["etag"]
        response = self.session.get(f"{API_URL}{path}", params=params, headers=headers, timeout=TIMEOUT)
        if response.status_code == HTTPStatus.NOT_MODIFIED and cached:
            text = cached["text"]
        else:
            response.raise_for_status()
            text = response.text
            if self.cache is not None and (etag := response.headers.get("ETag")):
                try:
                    self.cache.set(key, json.dumps({"etag": etag, "text": text}), ex=3600)
                except RedisError:
                    logger.warning("GitHub dashboard ETag cache write failed", exc_info=True)
        return text

    def get(self, path: str, **params: str | int) -> dict:
        """Read a JSON object, refusing an unexpected upstream response shape."""
        data = json.loads(self.read(path, **params))
        if not isinstance(data, dict):
            msg = "GitHub returned an unexpected response shape."
            raise DashboardError(msg)
        return data

    def pages(self, path: str, key: str | None = None, **params: str | int) -> list[dict]:
        """Read all pages of a list endpoint, including nested lists such as check_runs."""
        found = []
        page = 1
        while True:
            data = json.loads(self.read(path, **params, per_page=PAGE_SIZE, page=page))
            batch = data[key] if key else data
            if not isinstance(batch, list):
                msg = "GitHub returned an unexpected list response."
                raise DashboardError(msg)
            found.extend(batch)
            if len(batch) < PAGE_SIZE:
                return found
            page += 1

    def repositories(self) -> list[dict]:
        """Return all accessible, non-archived repositories owned by the token's user."""
        login = self.get("/user")["login"]
        return [
            {key: repo[key] for key in ("full_name", "description", "html_url", "default_branch")}
            for repo in self.pages("/user/repos", affiliation="owner", sort="pushed")
            if not repo["archived"] and repo["owner"]["login"].casefold() == login.casefold()
        ]

    def owned_repository(self, owner: str, name: str) -> dict:
        """Recheck ownership and archival status before reading or publishing a repository."""
        user, repo = _parallel([lambda: self.get("/user"), lambda: self.get(f"/repos/{owner}/{name}")])
        login = user["login"]
        if repo["owner"]["login"].casefold() != login.casefold() or repo["archived"]:
            msg = "Only non-archived repositories owned by your GitHub account are supported."
            raise DashboardError(msg)
        return repo

    def project(self, repo: dict, known: Iterable[int] = (), section: str = "all") -> dict:
        """Return a project's open PRs and/or unfinished release workflows, without a recency cutoff.

        A section's key is only present when it was read.
        """
        path = f"/repos/{repo['full_name']}"
        sections = {
            "pulls": lambda: self.pulls(path),
            "workflows": lambda: self.release_workflows(repo, known),
        }
        wanted = list(sections) if section == "all" else [section]
        return dict(zip(wanted, _parallel(sections[name] for name in wanted), strict=True))

    def pulls(self, path: str) -> list[dict]:
        """Return every open PR with its checks, leaving out any that closed while being read."""
        pulls = _parallel(
            [lambda number=pr["number"]: self.pull(path, number) for pr in self.pages(f"{path}/pulls", state="open")]
        )
        return [pr for pr in pulls if pr is not None]

    def release_workflows(self, repo: dict, known: Iterable[int] = ()) -> list[dict]:
        """Return every unfinished release run drawn as a graph, then the latest finished one."""
        path = f"/repos/{repo['full_name']}"
        # Query each unfinished status: a first page of recent runs would hide old approval waits.
        # One after another and in lifecycle order, so a run that moves on during the sweep (say, just approved) is
        # found by a later query; queries made at once could each miss it.
        runs = {}
        for status in ("requested", "pending", "waiting", "queued", "in_progress"):
            for run in self.pages(f"{path}/actions/runs", "workflow_runs", status=status):
                runs[run["id"]] = run
        # The status filters can briefly lag a state change, so look up runs the dashboard showed last time directly.
        missed = [run_id for run_id in known if run_id not in runs]
        for run in _parallel([lambda run_id=run_id: self.run(path, run_id) for run_id in missed]):
            if run:
                runs[run["id"]] = run
        shown = [run for run in runs.values() if run["status"] != "completed" and _is_release(run)]
        # The latest finished release stays too, so its outcome is still there after looking away. A run rechecked
        # by ID may have finished more recently than the per-workflow lists say yet.
        finished = [run for run in runs.values() if run["status"] == "completed" and _is_release(run)]
        if latest := max([*finished, *self.finished_releases(path)], key=lambda run: run["id"], default=None):
            shown.append(latest)
        return _parallel([lambda run=run: self.workflow(repo, run) for run in shown])

    def finished_releases(self, path: str) -> list[dict]:
        """Return the latest finished run of each release workflow."""
        workflows = [
            workflow for workflow in self.pages(f"{path}/actions/workflows", "workflows") if _is_release(workflow)
        ]
        latest = _parallel(
            [
                lambda workflow=workflow: self.get(
                    f"{path}/actions/workflows/{workflow['id']}/runs", status="completed", per_page=1
                )["workflow_runs"]
                for workflow in workflows
            ]
        )
        return [run for runs in latest for run in runs]

    def release_run(self, repo: dict, run_id: int) -> dict | None:
        """Read one release run drawn as a graph, or None when it is gone or isn't a release."""
        run = self.run(f"/repos/{repo['full_name']}", run_id)
        return self.workflow(repo, run) if run and _is_release(run) else None

    def run(self, path: str, run_id: int) -> dict | None:
        """Read one workflow run, or None when it has been deleted."""
        try:
            return self.get(f"{path}/actions/runs/{run_id}")
        except requests.HTTPError as error:
            if error.response is not None and error.response.status_code == HTTPStatus.NOT_FOUND:
                return None
            raise

    def pull(self, path: str, number: int) -> dict | None:
        """Read mergeability and both Checks API and legacy commit-status checks for an open PR head; None if closed."""
        pr = self.get(f"{path}/pulls/{number}")
        if pr["state"] != "open":
            return None
        sha = pr["head"]["sha"]
        check_runs, status_list = _parallel(
            [
                lambda: self.pages(f"{path}/commits/{sha}/check-runs", "check_runs", filter="latest"),
                lambda: self.pages(f"{path}/commits/{sha}/statuses"),
            ]
        )
        checks = [
            {
                "name": check["name"],
                "state": check.get("conclusion") or check["status"],
                "pending": check["status"] != "completed",
                "url": check.get("html_url"),
            }
            for check in check_runs
        ]
        # The statuses endpoint is newest first, including superseded updates of the same context.
        statuses = {}
        for status in status_list:
            statuses.setdefault(status["context"], status)
        checks.extend(
            {
                "name": status["context"],
                "state": status["state"],
                "pending": status["state"] == "pending",
                "url": status.get("target_url"),
            }
            for status in statuses.values()
        )
        return {
            "number": number,
            "title": pr["title"],
            "url": pr["html_url"],
            "author": pr["user"]["login"],
            "draft": pr["draft"],
            "head": pr["head"]["ref"],
            "base": pr["base"]["ref"],
            "mergeable": pr["mergeable"],
            "merge_state": pr["mergeable_state"],
            "created_at": pr["created_at"],
            "checks": checks,
            "refresh": pr["mergeable"] is None or any(check["pending"] for check in checks),
        }

    def workflow(self, repo: dict, run: dict) -> dict:
        """Draw dependencies from the workflow at the run's commit, retaining every returned job and step."""
        path = f"/repos/{repo['full_name']}"
        jobs, text, pending_deployments = _parallel(
            [
                lambda: self.pages(f"{path}/actions/runs/{run['id']}/attempts/{run['run_attempt']}/jobs", "jobs"),
                lambda: self.read(f"{path}/contents/{run['path'].split('@')[0]}", raw=True, ref=run["head_sha"]),
                lambda: self.pending_deployments(repo, run["id"]) if run["status"] != "completed" else [],
            ]
        )
        try:
            definitions = workflow_graph.workflow_jobs(text)
        except ValueError as error:
            logger.warning("Cannot parse %s's workflow %s", repo["full_name"], run["path"], exc_info=True)
            msg = "The workflow file could not be parsed. Check the server logs for details."
            raise DashboardError(msg) from error
        return {
            "id": run["id"],
            "name": run["name"],
            "number": run["run_number"],
            "attempt": run["run_attempt"],
            "branch": run["head_branch"],
            "status": run["status"],
            "conclusion": run.get("conclusion"),
            "finished_at": run["updated_at"] if run["status"] == "completed" else None,
            "url": run["html_url"],
            "pending_deployments": pending_deployments,
            "nodes": workflow_graph.nodes(definitions, jobs, list_jobs=True),
            "jobs": [
                {
                    "name": job["name"],
                    "status": job["status"],
                    "conclusion": job["conclusion"],
                    "url": job["html_url"],
                    "steps": [
                        {key: step.get(key) for key in ("name", "status", "conclusion")}
                        for step in job.get("steps", [])
                    ],
                }
                for job in jobs
            ],
        }

    def pending_deployments(self, repo: dict, run_id: int) -> list[dict]:
        """Return approval gates and whether the PAT's user is allowed to release each gate."""
        return [
            {
                "id": deployment["environment"]["id"],
                "name": deployment["environment"]["name"],
                "can_approve": deployment["current_user_can_approve"],
                "wait_timer": deployment["wait_timer"],
            }
            for deployment in self.pages(f"/repos/{repo['full_name']}/actions/runs/{run_id}/pending_deployments")
        ]

    def approve_deployment(self, repo: dict, run_id: int, environment_id: int) -> None:
        """Recheck the release run and reviewer eligibility, then approve only the requested environment."""
        path = f"/repos/{repo['full_name']}/actions/runs/{run_id}"
        run = self.get(path)
        if run["status"] == "completed" or not _is_release(run):
            msg = "Only unfinished release workflows can be approved from this dashboard."
            raise DashboardError(msg)
        pending = self.pending_deployments(repo, run_id)
        deployment = next((item for item in pending if item["id"] == environment_id), None)
        if deployment is None:
            msg = "This deployment is no longer waiting for approval. Refresh the dashboard."
            raise DashboardError(msg)
        if not deployment["can_approve"]:
            msg = "Your GitHub account cannot approve this environment. Check its required-reviewer rules."
            raise DashboardError(msg)
        self.post(
            f"{path}/pending_deployments",
            {
                "environment_ids": [environment_id],
                "state": "approved",
                "comment": "Approved from the personal GitHub dashboard.",
            },
        )

    def publish_release(self, repo: dict) -> dict:
        """Generate notes and immediately publish the next minor release on the default branch."""
        path = f"/repos/{repo['full_name']}"
        releases = [release for release in self.pages(f"{path}/releases") if not release["draft"]]
        previous, tag = next_release(releases)
        # Do not accidentally release an existing tag pointing at an older commit.
        response = self.session.get(f"{API_URL}{path}/git/ref/tags/{tag}", timeout=TIMEOUT)
        if response.status_code != HTTPStatus.NOT_FOUND:
            response.raise_for_status()
            msg = f"Tag {tag} already exists. Resolve it on GitHub before publishing."
            raise DashboardError(msg)
        payload = {"tag_name": tag, "target_commitish": repo["default_branch"]}
        if previous:
            payload["previous_tag_name"] = previous
        notes = self.post(f"{path}/releases/generate-notes", payload).json()
        return self.post(
            f"{path}/releases",
            {
                "tag_name": tag,
                "target_commitish": repo["default_branch"],
                "name": notes["name"],
                "body": notes["body"],
                "draft": False,
                "prerelease": False,
            },
        ).json()

    def post(self, path: str, payload: dict) -> requests.Response:
        """Send a GitHub write, never retrying a potentially completed action."""
        response = self.session.post(f"{API_URL}{path}", json=payload, timeout=TIMEOUT)
        response.raise_for_status()
        return response


SEMVER = re.compile(
    r"^(v?)(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)"
    r"(?:-([0-9A-Za-z.-]+))?(?:\+[0-9A-Za-z.-]+)?$"
)


def next_release(releases: list[dict]) -> tuple[str | None, str]:
    """Return the highest semver release's tag (prereleases count) and the next minor version, keeping its v prefix."""
    if not releases:
        return None, "1.0.0"
    versions = [(match, release) for release in releases if (match := SEMVER.fullmatch(release["tag_name"]))]
    if not versions:
        msg = "No release has a semantic version tag (for example 1.19.0 or v1.19.0)."
        raise DashboardError(msg)
    match, release = max(
        versions,
        key=lambda pair: (*map(int, pair[0].group(2, 3, 4)), pair[0].group(5) is None),
    )
    return release["tag_name"], f"{match[1]}{int(match[2])}.{int(match[3]) + 1}.0"
