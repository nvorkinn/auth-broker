import json
import os
import re
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
HEAD = "a" * 40
REPO = "example/consumer"
# BaseLoader only loads strings, preserving GitHub's "on" key instead of a YAML 1.1 boolean.
WORKFLOWS = {
    name: yaml.load((ROOT / ".github/workflows" / name).read_text(), Loader=yaml.BaseLoader)  # noqa: S506
    for name in ("all-checks.yml", "auto_approve.yml", "create_global_pr_status.yml")
}
APPROVAL = WORKFLOWS["auto_approve.yml"]["jobs"]["auto-approve"]
SWEEP = APPROVAL["steps"][0]

PULLS = f"GET repos/{REPO}/pulls?state=open&per_page=100"


def _check_runs(sha):
    return f"GET repos/{REPO}/commits/{sha}/check-runs?check_name=Global%20PR%20Status&filter=latest"


def _reviews(number):
    return f"GET repos/{REPO}/pulls/{number}/reviews"


def _pull(number):
    return f"GET repos/{REPO}/pulls/{number}"


def _approve(number):
    return f"POST repos/{REPO}/pulls/{number}/reviews"


@pytest.fixture
def run_sweep(tmp_path):
    """Run the approval step against a fake gh that answers each request from a {"METHOD url": response} table."""
    log = tmp_path / "requests.jsonl"
    gh = tmp_path / "gh"
    gh.write_text(
        f"#!{sys.executable}\n"
        + textwrap.dedent("""\
            import json
            import os
            import subprocess
            import sys
            from pathlib import Path

            args = sys.argv[1:]
            method = args[args.index("--method") + 1] if "--method" in args else "GET"
            key = f"{method} {next(arg for arg in args if arg.startswith('repos/'))}"
            with Path(os.environ["REQUEST_LOG"]).open("a") as stream:
                stream.write(json.dumps({"key": key, "args": args}) + "\\n")
            routes = json.loads(os.environ["ROUTES"])
            if key not in routes:
                sys.exit(f"Unexpected request: {key}")
            response = routes[key]
            if response == "error":
                sys.exit("GitHub API request failed")
            query = args[args.index("--jq") + 1]
            result = subprocess.run(["jq", "-r", query], input=json.dumps(response), text=True, check=True)
            sys.exit(result.returncode)
            """)
    )
    gh.chmod(0o755)

    def run(routes):
        log.write_text("")
        env = {
            **os.environ,
            "PATH": f"{tmp_path}{os.pathsep}{os.environ['PATH']}",
            "REQUEST_LOG": str(log),
            "ROUTES": json.dumps(routes),
            "GH_TOKEN": "test-token",
            "REPO": REPO,
        }
        assert SWEEP["shell"] == "bash"
        result = subprocess.run(  # noqa: S603
            ["bash", "-e", "-o", "pipefail", "-c", SWEEP["run"]],  # noqa: S607
            env=env,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        return result, [json.loads(line) for line in log.read_text().splitlines()]

    return run


def _pr(number=7, sha=HEAD, *, draft=False, repo=REPO, user_type="User"):
    return {
        "number": number,
        "draft": draft,
        "head": {"sha": sha, "repo": {"full_name": repo}},
        "user": {"type": user_type},
    }


def _status(status="completed", conclusion="success"):
    """Return the status check's runs: an older one that failed, then the latest."""
    return {
        "check_runs": [
            {"id": 1, "status": "completed", "conclusion": "failure"},
            {"id": 2, "status": status, "conclusion": conclusion},
        ]
    }


def _routes(*prs, check=None, reviews=(), head=None):
    """Everything a sweep asks for, for each open PR: it passed, has no approval from us, and its head hasn't moved."""
    routes = {PULLS: list(prs)}
    for pr in prs:
        sha, number = pr["head"]["sha"], pr["number"]
        routes[_check_runs(sha)] = check or _status()
        routes[_reviews(number)] = list(reviews)
        routes[_pull(number)] = {"head": {"sha": head or sha}}
        routes[_approve(number)] = {"state": "APPROVED"}
    return routes


def _approvals(requests):
    return [request for request in requests if request["key"].startswith("POST")]


def test_workflows_keep_their_triggers_and_permissions():
    approval = WORKFLOWS["auto_approve.yml"]
    assert approval["on"]["workflow_run"] == {"workflows": ["All checks"], "types": ["completed"]}
    assert "workflow_call" in approval["on"]
    assert approval["permissions"] == {"contents": "read", "pull-requests": "write", "checks": "read"}
    assert approval["concurrency"]["cancel-in-progress"] == "false"
    assert APPROVAL["if"] == "github.event.workflow_run.conclusion == 'success'"

    gate = WORKFLOWS["all-checks.yml"]
    assert gate["on"].keys() == {"workflow_run"}
    assert gate["on"]["workflow_run"]["types"] == ["completed"]
    assert gate["permissions"] == {"actions": "read", "checks": "write"}

    create = WORKFLOWS["create_global_pr_status.yml"]
    assert create["on"].keys() == {"pull_request"}
    assert create["permissions"] == {"checks": "write"}

    # The shared logic is pinned to a commit, not a branch.
    for caller, job in ((gate, "all-checks"), (create, "create-global-pr-status")):
        assert re.fullmatch(
            r"nvorkinn/\.github/\.github/workflows/[\w-]+\.yml@[0-9a-f]{40}", caller["jobs"][job]["uses"]
        )


def test_every_workflow_that_runs_on_pull_requests_triggers_the_gate():
    listed = set(WORKFLOWS["all-checks.yml"]["on"]["workflow_run"]["workflows"])
    # Auto-approve runs after the gate, which can't wait for it.
    exempt = {"All checks", "Auto-approve PRs"}
    on_pull_requests = set()
    for path in (ROOT / ".github/workflows").glob("*.yml"):
        workflow = yaml.load(path.read_text(), Loader=yaml.BaseLoader)  # noqa: S506
        if "pull_request" in workflow["on"]:
            on_pull_requests.add(workflow["name"])
    assert on_pull_requests - exempt <= listed
    assert "Create Global PR Status" in listed


def test_approval_never_checks_out_code():
    assert not any(step.get("uses", "").startswith("actions/checkout") for step in APPROVAL["steps"])


def test_sweep_approves_a_pr_whose_status_check_succeeded_on_its_head(run_sweep):
    result, requests = run_sweep(_routes(_pr()))
    assert result.returncode == 0, result.stderr
    (approval,) = _approvals(requests)
    assert approval["key"] == _approve(7)
    assert "event=APPROVE" in approval["args"]
    assert f"commit_id={HEAD}" in approval["args"]
    assert "#7: APPROVED" in result.stdout


def test_sweep_uses_the_newest_status_check_for_the_head(run_sweep):
    result, requests = run_sweep(_routes(_pr(), check=_status("in_progress", None)))
    assert result.returncode == 0, result.stderr
    assert not _approvals(requests)


@pytest.mark.parametrize(
    "pr",
    [_pr(draft=True), _pr(repo="someone/fork"), _pr(user_type="Bot")],
    ids=["draft", "fork", "bot"],
)
def test_sweep_skips_drafts_forks_and_bots_without_looking_further(run_sweep, pr):
    result, requests = run_sweep({PULLS: [pr]})
    assert result.returncode == 0, result.stderr
    assert [request["key"] for request in requests] == [PULLS]


@pytest.mark.parametrize(
    "check",
    [_status("in_progress", None), _status(conclusion="failure"), _status(conclusion="cancelled"), {"check_runs": []}],
    ids=["running", "failure", "cancelled", "none"],
)
def test_sweep_waits_for_the_status_check_to_succeed(run_sweep, check):
    result, requests = run_sweep({PULLS: [_pr()], _check_runs(HEAD): check})
    assert result.returncode == 0, result.stderr
    assert "Global PR Status hasn't succeeded" in result.stdout
    assert not _approvals(requests)


def test_sweep_does_not_approve_the_same_commit_twice(run_sweep):
    ours = {"user": {"login": "github-actions[bot]"}, "state": "APPROVED", "commit_id": HEAD}
    result, requests = run_sweep(_routes(_pr(), reviews=[ours]))
    assert result.returncode == 0, result.stderr
    assert "already approved" in result.stdout
    assert not _approvals(requests)


@pytest.mark.parametrize(
    "review",
    [
        {"user": {"login": "github-actions[bot]"}, "state": "APPROVED", "commit_id": "b" * 40},
        {"user": {"login": "github-actions[bot]"}, "state": "DISMISSED", "commit_id": HEAD},
        {"user": {"login": "someone"}, "state": "APPROVED", "commit_id": HEAD},
    ],
    ids=["older-commit", "dismissed", "someone-else"],
)
def test_sweep_approves_unless_it_already_approved_this_commit(run_sweep, review):
    result, requests = run_sweep(_routes(_pr(), reviews=[review]))
    assert result.returncode == 0, result.stderr
    assert len(_approvals(requests)) == 1


def test_sweep_refuses_a_head_that_changed_while_checking(run_sweep):
    result, requests = run_sweep(_routes(_pr(), head="b" * 40))
    assert result.returncode == 0, result.stderr
    assert "::warning::#7's head changed" in result.stdout
    assert not _approvals(requests)


def test_sweep_handles_each_open_pr_on_its_own_commit(run_sweep):
    routes = _routes(_pr(1, "1" * 40), _pr(2, "2" * 40))
    routes[_check_runs("2" * 40)] = _status(conclusion="failure")
    result, requests = run_sweep(routes)
    assert result.returncode == 0, result.stderr
    (approval,) = _approvals(requests)
    assert approval["key"] == _approve(1)
    assert f"commit_id={'1' * 40}" in approval["args"]


def test_sweep_with_no_open_prs_does_nothing(run_sweep):
    result, requests = run_sweep({PULLS: []})
    assert result.returncode == 0, result.stderr
    assert [request["key"] for request in requests] == [PULLS]


@pytest.mark.parametrize("failing", ["pulls", "check", "reviews", "pull", "approve"])
def test_sweep_propagates_api_failures(run_sweep, failing):
    routes = _routes(_pr())
    key = {
        "pulls": PULLS,
        "check": _check_runs(HEAD),
        "reviews": _reviews(7),
        "pull": _pull(7),
        "approve": _approve(7),
    }[failing]
    routes[key] = "error"
    result, _ = run_sweep(routes)
    assert result.returncode != 0
    assert "GitHub API request failed" in result.stderr
