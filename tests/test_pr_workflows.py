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
# BaseLoader only loads strings, preserving GitHub's "on" key instead of a YAML 1.1 boolean.
WORKFLOWS = {
    name: yaml.load((ROOT / ".github/workflows" / name).read_text(), Loader=yaml.BaseLoader)  # noqa: S506
    for name in ("all-checks.yml", "auto_approve.yml", "create_global_pr_status.yml")
}
APPROVAL = WORKFLOWS["auto_approve.yml"]["jobs"]["auto-approve"]


@pytest.fixture
def run_step(tmp_path):
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
            if "--method" in args:
                key = "reviews"
            else:
                key = "pr"
            log = Path(os.environ["REQUEST_LOG"])
            previous = [json.loads(line) for line in log.read_text().splitlines()]
            index = sum(request["key"] == key for request in previous)
            with log.open("a") as stream:
                stream.write(json.dumps({"key": key, "args": args}) + "\\n")
            if index >= 10:
                sys.exit("Polling did not settle")
            if key == "download":
                directory = Path(args[args.index("--dir") + 1])
                directory.mkdir(parents=True, exist_ok=True)
                (directory / "check-run-id").write_text(os.environ["CHECK_RUN_ID"] + "\\n")
                sys.exit(0)
            responses = json.loads(os.environ["API_RESPONSES"])[key]
            response = responses[min(index, len(responses) - 1)]
            if response == "error":
                sys.exit("GitHub API request failed")
            if "--jq" in args:
                query = args[args.index("--jq") + 1]
                result = subprocess.run(
                    ["jq", "-r", query], input=json.dumps(response), text=True, check=True
                )
                sys.exit(result.returncode)
            pages = response if isinstance(response, list) else [response]
            for page in pages:
                print(json.dumps(page))
            """)
    )
    gh.chmod(0o755)
    sleep = tmp_path / "sleep"
    sleep.write_text("#!/bin/sh\nexit 0\n")
    sleep.chmod(0o755)

    def run(step, responses=None, **overrides):
        log.write_text("")
        env = {
            **os.environ,
            "PATH": f"{tmp_path}{os.pathsep}{os.environ['PATH']}",
            "REQUEST_LOG": str(log),
            "API_RESPONSES": json.dumps(
                {
                    "pr": [{"head": {"sha": HEAD}}],
                    "reviews": [{"state": "APPROVED"}],
                    **(responses or {}),
                }
            ),
            "GH_TOKEN": "test-token",
            "REPO": "example/consumer",
            "PR_NUMBER": "7",
            "SHA": HEAD,
            "AUTO_APPROVE_WORKFLOW": "Auto-approve PRs",
            **overrides,
        }
        assert step["shell"] == "bash"
        result = subprocess.run(  # noqa: S603
            ["bash", "-e", "-o", "pipefail", "-c", step["run"]],  # noqa: S607
            env=env,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
            cwd=tmp_path,
        )
        return result, [json.loads(line) for line in log.read_text().splitlines()]

    return run


def test_workflows_keep_their_triggers_and_permissions():
    approval = WORKFLOWS["auto_approve.yml"]
    assert {"pull_request", "workflow_call"} <= approval["on"].keys()
    assert approval["permissions"]["actions"] == "read"
    assert approval["permissions"]["checks"] == "read"
    assert approval["permissions"]["contents"] == "read"
    assert approval["permissions"]["pull-requests"] == "write"
    assert approval["on"]["pull_request"]["types"] == ["opened", "synchronize", "reopened", "ready_for_review"]

    gate = WORKFLOWS["all-checks.yml"]
    assert gate["on"].keys() == {"workflow_run"}
    assert gate["on"]["workflow_run"]["types"] == ["completed"]
    assert gate["permissions"] == {"actions": "read", "checks": "write"}
    create_file = gate["jobs"]["all-checks"]["with"]["create-workflow-file"]
    assert (ROOT / ".github/workflows" / create_file).read_text().startswith("name: Create Global PR Status")

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
    # Auto-approve waits for the status check this completes, and the gate can't wait for itself.
    exempt = {"All checks", "Auto-approve PRs"}
    on_pull_requests = set()
    for path in (ROOT / ".github/workflows").glob("*.yml"):
        workflow = yaml.load(path.read_text(), Loader=yaml.BaseLoader)  # noqa: S506
        if "pull_request" in workflow["on"]:
            on_pull_requests.add(workflow["name"])
    assert on_pull_requests - exempt <= listed
    assert "Create Global PR Status" in listed


def test_approval_never_checks_out_pr_code_and_skips_unwritable_or_self_authored_prs():
    assert not any(step.get("uses", "").startswith("actions/checkout") for step in APPROVAL["steps"])
    for guard in (
        "github.event_name == 'pull_request'",
        "!github.event.pull_request.draft",
        "github.event.pull_request.head.repo.full_name == github.repository",
        "github.actor != 'dependabot[bot]'",
        "github.event.pull_request.user.login != 'dependabot[bot]'",
        "github.event.pull_request.user.login != 'github-actions[bot]'",
    ):
        assert guard in APPROVAL["if"]


@pytest.mark.parametrize("prefix", ["", "caller / ", "outer / inner / "])
def test_approval_waiter_excludes_its_own_prefixed_check_but_keeps_the_gate(prefix):
    waiter = APPROVAL["steps"][0]["with"]
    pattern = waiter["check-regexp"]
    assert not re.search(pattern, f"{prefix}auto-approve")
    assert re.search(pattern, f"{prefix}all-checks")
    assert re.search(pattern, f"{prefix}auto-approve tests")
    assert waiter["allowed-conclusions"] == "success,skipped,neutral"


def test_approval_is_bound_to_the_head_that_passed_checks_without_a_checkout(run_step):
    result, requests = run_step(APPROVAL["steps"][-1])
    assert result.returncode == 0, result.stderr
    assert requests[-1]["key"] == "reviews"
    assert f"commit_id={HEAD}" in requests[-1]["args"]
    assert "event=APPROVE" in requests[-1]["args"]
    assert "repos/example/consumer/pulls/7/reviews" in requests[-1]["args"]


def test_approval_refuses_a_head_that_changed_while_waiting(run_step):
    result, requests = run_step(APPROVAL["steps"][-1], {"pr": [{"head": {"sha": "b" * 40}}]})
    assert result.returncode == 1
    assert "::error::The PR head changed" in result.stdout
    assert [request["key"] for request in requests] == ["pr"]
