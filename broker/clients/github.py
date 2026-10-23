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
from typing import TYPE_CHECKING, cast

import requests
import requests.adapters
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
# Check states that stop a direct merge, as the dashboard's PR list counts them.
FAILED_CHECKS = {"failure", "timed_out", "cancelled", "error", "startup_failure", "action_required", "stale"}
ENABLE_AUTO_MERGE = """
mutation($id: ID!, $method: PullRequestMergeMethod!) {
  enablePullRequestAutoMerge(input: {pullRequestId: $id, mergeMethod: $method}) { clientMutationId }
}
"""
DISABLE_AUTO_MERGE = """
mutation($id: ID!) {
  disablePullRequestAutoMerge(input: {pullRequestId: $id}) { clientMutationId }
}
"""
# A workflow is a release if its name says so.
RELEASE = re.compile(r"release|deploy", re.IGNORECASE)
# PRs opened by a Claude Code session end their description with a link to it.
CLAUDE_SESSION = re.compile(r"https://claude\.ai/code/(session_[A-Za-z0-9]+)")
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
# A PR's changed files shown on the dashboard; the rest are left to GitHub's own Files tab.
MAX_FILES = 300
# How long an answer GitHub never changes (a finished run's graph, a file at a commit) is kept without asking again.
FIXED_TTL_S = 86400
# Runs read per release workflow: enough for the latest one and any just before it that hasn't finished.
RECENT_RELEASES = 5


def summary(token: str, cache: Redis | None = None) -> dict:
    """Return the owner's GitHub at a glance, the developer panel's `github`.

    Read through the dashboard's ETag `cache` when given, so the calls whose answer hasn't changed since the last poll
    cost none of the token's rate limit. Raises requests.HTTPError if GitHub refuses the token or fails.
    """
    api = Dashboard(token, cache)
    try:
        login = api.get("/user")["login"]
        repos = [
            repo for repo in api.pages("/user/repos", affiliation="owner", sort="pushed") if not repo.get("archived")
        ]
        recent = _recently_pushed(repos)
        return {
            "login": login,
            "repos": len(repos),
            "open_prs": _count(api, f"is:pr is:open author:{login}"),
            "review_requests": _count(api, f"is:pr is:open review-requested:{login}"),
            "failing": [repo["name"] for repo in recent if _failing(api, repo)],
            "releases": _releases(api, recent),
        }
    finally:
        api.close()


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


def _count(api: Dashboard, query: str) -> int:
    return api.get("/search/issues", q=query, per_page=1)["total_count"]


def _recently_pushed(repos: list[dict]) -> list[dict]:
    since = datetime.now(UTC) - ACTIVE
    recent = [repo for repo in repos if repo.get("pushed_at") and datetime.fromisoformat(repo["pushed_at"]) >= since]
    return recent[:MOST_CHECKED]


def _failing(api: Dashboard, repo: dict) -> bool:
    """Whether any workflow's latest finished run on the repo's default branch failed."""
    runs = api.get(
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


def _releases(api: Dashboard, repos: list[dict]) -> list[dict]:
    """Return each repo's release that's running or finished in the last SHOWN_AFTER, the latest started first.

    A repo whose release can't be read is left out, so the rest of the panel still shows; but a rate limit is raised
    (requests.HTTPError), as every call after it would fail too and the panel would lose its releases for nothing.
    """
    now = datetime.now(UTC)
    found = []
    for repo in repos:
        try:
            release = _release(api, repo, now)
        except requests.RequestException as e:
            if rate_limit_wait(e) is not None:
                raise
            logger.warning("Couldn't read %s's release: %s", repo["full_name"], e)
            continue
        if release is not None:
            found.append(release)
    return sorted(found, key=lambda release: release["started"], reverse=True)


def _release(api: Dashboard, repo: dict, now: datetime) -> dict | None:
    """Return the repo's latest release run with its jobs as graph nodes; None if none, or it finished a while ago."""
    runs = api.get(
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
    jobs = api.get(f"/repos/{repo['full_name']}/actions/runs/{run['id']}/jobs", per_page=100)["jobs"]
    return {
        "repo": repo["name"],
        "title": _title(run, repo),
        "state": workflow_graph.run_state(run),
        "started": run["run_started_at"],
        "finished": run["updated_at"] if finished else None,
        "jobs": workflow_graph.nodes(_workflow_jobs(api, repo, run), jobs),
    }


def _claude_session(body: str | None) -> str | None:
    """Return the ID of the Claude Code session a PR description links to last, if any."""
    sessions = CLAUDE_SESSION.findall(body or "")
    return sessions[-1] if sessions else None


def _merge_method(repo: dict) -> str:
    """Pick the first merge method the repository allows, preferring merge commits."""
    for method, setting in (
        ("MERGE", "allow_merge_commit"),
        ("SQUASH", "allow_squash_merge"),
        ("REBASE", "allow_rebase_merge"),
    ):
        if repo.get(setting, True):
            return method
    return "MERGE"


def _is_release(run: dict) -> bool:
    """Tell whether a workflow, or a run of one, is a release the dashboard draws."""
    return is_release_name(run["name"])


def is_release_name(name: str | None) -> bool:
    """Tell whether a workflow's name makes it a release the dashboard draws."""
    return "release" in (name or "").casefold()


def _title(run: dict, repo: dict) -> str:
    """Return the tag a release was pushed for, else its run number (one started by hand runs on the default branch)."""
    if run["event"] == "push" and run["head_branch"] != repo["default_branch"]:
        return run["head_branch"]
    return f"#{run['run_number']}"


def _workflow_jobs(api: Dashboard, repo: dict, run: dict) -> list[workflow_graph.WorkflowJob]:
    """Return the jobs of the run's workflow file, as at the run's commit; none if it can't be read.

    Without them the graph still shows the run's jobs, but not the lines between them. A rate limit is raised
    (requests.HTTPError), as in _releases.
    """
    path = run["path"].split("@")[0]
    try:
        return workflow_graph.workflow_jobs(
            api.read(f"/repos/{repo['full_name']}/contents/{path}", raw=True, ref=run["head_sha"])
        )
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
    """The personal dashboard's GitHub operations, all performed server-side as its signed-in user."""

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

    def remembered[T](self, parts: list, compute: Callable[[], T]) -> T:
        """Return what `compute` gave for `parts` before, else compute and keep it: for answers that never change.

        Unlike read(), nothing is sent to GitHub at all while it's kept. A failed compute raises and keeps nothing.
        """
        identity = json.dumps([self.cache_scope, *parts], sort_keys=True)
        key = f"github-dashboard:fixed:{hashlib.sha256(identity.encode()).hexdigest()}"
        if self.cache is not None:
            try:
                if (value := self.cache.get(key)) is not None:
                    return json.loads(value)
            except RedisError:
                logger.warning("GitHub dashboard fixed cache read failed", exc_info=True)
        result = compute()
        if self.cache is not None:
            try:
                self.cache.set(key, json.dumps(result), ex=FIXED_TTL_S)
            except RedisError:
                logger.warning("GitHub dashboard fixed cache write failed", exc_info=True)
        return result

    def get(self, path: str, **params: str | int) -> dict:
        """Read a JSON object, refusing an unexpected upstream response shape."""
        data = json.loads(self.read(path, raw=False, **params))
        if not isinstance(data, dict):
            msg = "GitHub returned an unexpected response shape."
            raise DashboardError(msg)
        return data

    def pages(self, path: str, key: str | None = None, **params: str | int) -> list[dict]:
        """Read all pages of a list endpoint, including nested lists such as check_runs."""
        found = []
        page = 1
        while True:
            data = json.loads(self.read(path, raw=False, **params, per_page=PAGE_SIZE, page=page))
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

    def project(self, repo: dict, section: str = "all") -> dict:
        """Return a project's open PRs and/or unfinished release workflows, without a recency cutoff.

        A section's key is only present when it was read.
        """
        path = f"/repos/{repo['full_name']}"
        sections = {
            "pulls": lambda: self.pulls(path),
            "workflows": lambda: self.release_workflows(repo),
        }
        wanted = list(sections) if section == "all" else [section]
        return dict(zip(wanted, _parallel(sections[name] for name in wanted), strict=True))

    # Every open PR with its head's checks in one query, newest first like REST's list. It costs 1 (pullRequests) +
    # 100 (commits, one per PR) + 100 (contexts, one per commit) requests = 201 / 100 = 2 points a page of 100 PRs, from
    # GraphQL's own 5,000 points an hour. GraphQL has no ETags, so an unchanged read costs as much as a changed one.
    PULLS_QUERY = """
query($owner: String!, $name: String!, $first: Int!, $after: String) {
  repository(owner: $owner, name: $name) {
    pullRequests(states: OPEN, first: $first, after: $after, orderBy: {field: CREATED_AT, direction: DESC}) {
      pageInfo { hasNextPage endCursor }
      nodes {
        number title url isDraft headRefName baseRefName mergeable mergeStateStatus createdAt body headRefOid
        author { login }
        autoMergeRequest { enabledAt }
        commits(last: 1) { nodes { commit { statusCheckRollup { contexts(first: $first) { ...checks } } } } }
      }
    }
  }
}
"""
    # The rest of a head commit's checks, past the first page the PR query read (1 point a page).
    CHECKS_QUERY = """
query($owner: String!, $name: String!, $sha: GitObjectID!, $first: Int!, $after: String) {
  repository(owner: $owner, name: $name) {
    object(oid: $sha) { ... on Commit { statusCheckRollup { contexts(first: $first, after: $after) { ...checks } } } }
  }
}
"""
    CHECKS_FRAGMENT = """
fragment checks on StatusCheckRollupContextConnection {
  pageInfo { hasNextPage endCursor }
  nodes {
    __typename
    ... on CheckRun { databaseId name status conclusion url checkSuite { databaseId } }
    ... on StatusContext { context state targetUrl createdAt }
  }
}
"""

    def pulls(self, path: str) -> list[dict]:
        """Return every open PR with its checks, shaped like pull(), from GraphQL pages of PAGE_SIZE PRs."""
        owner, name = path.removeprefix("/repos/").split("/")
        found = []
        after = None
        while True:
            data = self.graphql(self.PULLS_QUERY, owner=owner, name=name, first=PAGE_SIZE, after=after)
            connection = data["repository"]["pullRequests"]
            found.extend(connection["nodes"])
            if not connection["pageInfo"]["hasNextPage"]:
                break
            after = connection["pageInfo"]["endCursor"]
        return [self._graphql_pull(owner, name, pr) for pr in found]

    def _graphql_pull(self, owner: str, name: str, pr: dict) -> dict:
        """Map one PR from PULLS_QUERY to pull()'s shape, with GraphQL's enums read as REST's values."""
        commits = pr["commits"]["nodes"]
        rollup = commits[0]["commit"]["statusCheckRollup"] if commits else None
        sha = pr["headRefOid"]
        checks = _rollup_checks(self._rollup_contexts(owner, name, sha, rollup["contexts"])) if rollup else []
        mergeable = GRAPHQL_MERGEABLE.get(pr["mergeable"])
        return {
            "number": pr["number"],
            "title": pr["title"],
            "url": pr["url"],
            # A deleted account's PR has no author in GraphQL; REST names it ghost.
            "author": (pr["author"] or {}).get("login", "ghost"),
            "draft": pr["isDraft"],
            "head": pr["headRefName"],
            "base": pr["baseRefName"],
            "mergeable": mergeable,
            # GraphQL's MergeStateStatus values are REST's mergeable_state in capitals (CLEAN, DIRTY, BLOCKED, ...).
            "merge_state": pr["mergeStateStatus"].lower(),
            "created_at": pr["createdAt"],
            "session": _claude_session(pr["body"]),
            "sha": sha,
            "auto_merge": pr["autoMergeRequest"] is not None,
            "checks": checks,
            "refresh": mergeable is None or any(check["pending"] for check in checks),
        }

    def _rollup_contexts(self, owner: str, name: str, sha: str, connection: dict) -> list[dict]:
        """Return all of a head commit's rollup contexts, reading any past the first page."""
        contexts = list(connection["nodes"])
        while connection["pageInfo"]["hasNextPage"]:
            variables = {"sha": sha, "first": PAGE_SIZE, "after": connection["pageInfo"]["endCursor"]}
            commit = self.graphql(self.CHECKS_QUERY, owner=owner, name=name, **variables)["repository"]["object"]
            # The branch moved on while being read: the PR's next read has the new head's checks.
            if not commit or not commit.get("statusCheckRollup"):
                break
            connection = commit["statusCheckRollup"]["contexts"]
            contexts.extend(connection["nodes"])
        return contexts

    def graphql(self, query: str, **variables: str | int | None) -> dict:
        """Run a GraphQL read and return its data; GitHub reports a refused query as a 200 with `errors`."""
        if "...checks" in query:
            query += self.CHECKS_FRAGMENT
        response = self.post("/graphql", {"query": query, "variables": variables})
        body = response.json()
        if not isinstance(body, dict):
            msg = "GitHub returned an unexpected response shape."
            raise DashboardError(msg)
        if errors := body.get("errors"):
            if any(error.get("type") == "RATE_LIMITED" for error in errors):
                # GraphQL's hourly limit can come as a 200 with X-RateLimit-Remaining 0 and its reset: read it as the
                # 429 it is, so rate_limit_wait (and the routes' 429) back off until then, as for REST.
                response.status_code = HTTPStatus.TOO_MANY_REQUESTS
                raise requests.HTTPError(response=response)
            msg = f"GitHub refused: {errors[0].get('message', '')}"
            raise DashboardError(msg)
        return body["data"]

    def release_workflows(self, repo: dict) -> list[dict]:
        """Draw each release workflow's unfinished recent runs, then the latest finished release."""
        runs = self.recent_releases(f"/repos/{repo['full_name']}")
        shown = [run for run in runs if run["status"] != "completed"]
        # The latest finished release stays too, so its outcome is still there after looking away.
        finished = [run for run in runs if run["status"] == "completed"]
        if latest := max(finished, key=lambda run: run["id"], default=None):
            shown.append(latest)
        return _parallel([lambda run=run: self.workflow(repo, run) for run in shown])

    def recent_releases(self, path: str) -> list[dict]:
        """Return the latest RECENT_RELEASES runs of each release workflow, in any status.

        Read per workflow, so CI runs can't push a release off the page; an older run still unfinished past those is
        left out, as a newer release replaces it.
        """
        workflows = [
            workflow for workflow in self.pages(f"{path}/actions/workflows", "workflows") if _is_release(workflow)
        ]
        latest = _parallel(
            [
                lambda workflow=workflow: self.get(
                    f"{path}/actions/workflows/{workflow['id']}/runs", per_page=RECENT_RELEASES
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
        """Read mergeability and both Checks API and legacy commit-status checks for an open PR head; None if closed.

        REST, not pulls()'s GraphQL: the quick polls repeat this for a busy PR, and an unchanged read is three 304s,
        which GitHub doesn't count, where a GraphQL read always costs a point.
        """
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
            "session": _claude_session(pr.get("body")),
            "sha": sha,
            "auto_merge": pr.get("auto_merge") is not None,
            "checks": checks,
            "refresh": pr["mergeable"] is None or any(check["pending"] for check in checks),
        }

    def pull_files(self, path: str, number: int) -> dict:
        """List an open PR's changed files with their patches; GitHub leaves out the patch of a very large file."""
        files = []
        page = 1
        while True:
            batch = json.loads(self.read(f"{path}/pulls/{number}/files", per_page=PAGE_SIZE, page=page))
            if not isinstance(batch, list):
                msg = "GitHub returned an unexpected list response."
                raise DashboardError(msg)
            files.extend(batch)
            if len(batch) < PAGE_SIZE or len(files) > MAX_FILES:
                break
            page += 1
        return {
            "files": [
                {
                    "filename": file["filename"],
                    "previous_filename": file.get("previous_filename"),
                    "status": file["status"],
                    "additions": file["additions"],
                    "deletions": file["deletions"],
                    "patch": file.get("patch"),
                    "url": file.get("blob_url"),
                }
                for file in files[:MAX_FILES]
            ],
            "truncated": len(files) > MAX_FILES,
        }

    def workflow(self, repo: dict, run: dict) -> dict:
        """Draw dependencies from the workflow at the run's commit, retaining every returned job and step.

        A finished attempt never changes (a re-run is a new attempt), so its drawing is kept rather than read again.
        """
        if run["status"] != "completed":
            return self._workflow(repo, run)
        parts = ["run", repo["full_name"], run["id"], run["run_attempt"], run["updated_at"]]
        return self.remembered(parts, lambda: self._workflow(repo, run))

    def _workflow(self, repo: dict, run: dict) -> dict:
        path = f"/repos/{repo['full_name']}"
        file = f"{path}/contents/{run['path'].split('@')[0]}"
        # One result type for all three: their union, so each is cast back to what its call returns.
        jobs, text, pending_deployments = _parallel(
            [
                lambda: self.pages(f"{path}/actions/runs/{run['id']}/attempts/{run['run_attempt']}/jobs", "jobs"),
                # A file at a commit never changes.
                lambda: self.remembered(
                    ["file", file, run["head_sha"]], lambda: self.read(file, raw=True, ref=run["head_sha"])
                ),
                lambda: self.pending_deployments(repo, run["id"]) if run["status"] != "completed" else [],
            ]
        )
        jobs, text = cast("list[dict]", jobs), cast("str", text)
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
        """Return approval gates and whether the token's user is allowed to release each gate."""
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

    def set_auto_merge(self, repo: dict, number: int, *, enabled: bool) -> dict:
        """Turn auto-merge on or off; with nothing required on the base branch, merge at once once checks are done."""
        path = f"/repos/{repo['full_name']}"
        pr = self.get(f"{path}/pulls/{number}")
        if pr["state"] != "open":
            msg = "This pull request is no longer open."
            raise DashboardError(msg)
        if enabled and pr["draft"]:
            msg = "Mark the pull request ready for review before turning on auto-merge."
            raise DashboardError(msg)
        if enabled and repo.get("allow_auto_merge") is False:
            msg = "Auto-merge is off for this repository. Turn on Allow auto-merge in its settings."
            raise DashboardError(msg)
        if (pr.get("auto_merge") is not None) == enabled:
            return {"auto_merge": enabled, "merged": False}
        method = _merge_method(repo)
        mutation = ENABLE_AUTO_MERGE if enabled else DISABLE_AUTO_MERGE
        variables = {"id": pr["node_id"], "method": method} if enabled else {"id": pr["node_id"]}
        errors = self.post("/graphql", {"query": mutation, "variables": variables}).json().get("errors")
        if not errors:
            return {"auto_merge": enabled, "merged": False}
        message = str(errors[0].get("message", ""))
        # GitHub won't wait when the base branch requires nothing, so a PR whose checks are done is merged now.
        if not enabled or "clean status" not in message.lower():
            msg = f"GitHub refused: {message}"
            raise DashboardError(msg)
        checks = (self.pull(path, number) or {}).get("checks", [])
        if any(check["pending"] or check["state"] in FAILED_CHECKS for check in checks):
            msg = (
                f"{pr['base']['ref']} requires no checks, so GitHub can't wait for them. "
                "Try again once this PR's checks pass."
            )
            raise DashboardError(msg)
        response = self.session.put(
            f"{API_URL}{path}/pulls/{number}/merge",
            json={"merge_method": method.lower(), "sha": pr["head"]["sha"]},
            timeout=TIMEOUT,
        )
        response.raise_for_status()
        return {"auto_merge": False, "merged": True}

    def post(self, path: str, payload: dict) -> requests.Response:
        """Send a GitHub write, never retrying a potentially completed action."""
        response = self.session.post(f"{API_URL}{path}", json=payload, timeout=TIMEOUT)
        response.raise_for_status()
        return response


# GraphQL's MergeableState as REST's mergeable; UNKNOWN (still being worked out) is REST's null.
GRAPHQL_MERGEABLE = {"MERGEABLE": True, "CONFLICTING": False}


def _rollup_checks(contexts: list[dict]) -> list[dict]:
    """Map a statusCheckRollup's contexts to pull()'s checks: check runs, then legacy commit statuses.

    The rollup is what the PR page shows: the latest run of each check and the latest status of each context, as
    REST's check-runs filter=latest and the newest of each context in /statuses. GitHub has been seen to return a
    superseded duplicate, though, so only the newest of each is kept: a check run by its suite (one per workflow run,
    whose re-runs stay in it) and name, newest by ID; a status by its context, newest by creation time.
    """
    runs: dict[tuple[int | None, str], dict] = {}
    for run in sorted(
        (item for item in contexts if item.get("__typename") == "CheckRun"), key=lambda run: run["databaseId"] or 0
    ):
        runs[(run["checkSuite"] or {}).get("databaseId"), run["name"]] = run
    statuses: dict[str, dict] = {}
    for status in sorted(
        (item for item in contexts if item.get("__typename") == "StatusContext"), key=lambda status: status["createdAt"]
    ):
        statuses[status["context"]] = status
    checks = [
        {
            "name": run["name"],
            "state": (run["conclusion"] or run["status"]).lower(),
            "pending": run["status"] != "COMPLETED",
            "url": run["url"],
        }
        for run in runs.values()
    ]
    # EXPECTED is a required context that hasn't reported yet: REST's statuses have no such state, so it's pending.
    checks.extend(
        {
            "name": status["context"],
            "state": "pending" if status["state"] == "EXPECTED" else status["state"].lower(),
            "pending": status["state"] in {"PENDING", "EXPECTED"},
            "url": status["targetUrl"],
        }
        for status in statuses.values()
    )
    return checks


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
