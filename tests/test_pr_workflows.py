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
GATE = WORKFLOWS["all-checks.yml"]["jobs"]["all-checks"]["steps"][0]
CREATE = WORKFLOWS["create_global_pr_status.yml"]["jobs"]["create-global-pr-status"]
CHECK_RUN_ID = "555"
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
            if args[:2] == ["run", "download"]:
                key = "download"
            elif any("/actions/workflows/" in arg for arg in args):
                key = "status_run"
            elif any("/actions/runs?" in arg for arg in args):
                key = "runs"
            elif any("/check-runs?" in arg for arg in args):
                key = "checks"
            elif "PATCH" in args:
                key = "patch"
            elif "--method" in args:
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
                    "status_run": [{"workflow_runs": [{"id": 9, "status": "completed"}]}],
                    "runs": [{"workflow_runs": []}],
                    "checks": [{"check_runs": []}],
                    "patch": [{"conclusion": "success"}],
                    "pr": [{"head": {"sha": HEAD}}],
                    "reviews": [{"state": "APPROVED"}],
                    **(responses or {}),
                }
            ),
            "CHECK_RUN_ID": CHECK_RUN_ID,
            "GH_TOKEN": "test-token",
            "REPO": "example/consumer",
            "PR_NUMBER": "7",
            "SHA": HEAD,
            "RUN_ID": "100",
            "WORKFLOW_NAME": "Custom gate",
            "IGNORED_CHECKS": GATE["env"]["IGNORED_CHECKS"],
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


def _run(run_id=1, name="Tests", status="completed", conclusion="success", event="pull_request"):
    return {
        "id": run_id,
        "workflow_id": 1,
        "event": event,
        "name": name,
        "status": status,
        "conclusion": conclusion,
        "html_url": f"https://github.com/example/consumer/actions/runs/{run_id}",
    }


def _check(check_id=1, name="test", status="completed", conclusion="success"):
    return {
        "id": check_id,
        "name": name,
        "status": status,
        "conclusion": conclusion,
        "html_url": "https://example.com/check",
    }


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
    assert "pull_request" in gate["jobs"]["all-checks"]["if"]
    assert "dynamic" in gate["jobs"]["all-checks"]["if"]

    create = WORKFLOWS["create_global_pr_status.yml"]
    assert create["on"].keys() == {"pull_request"}
    assert create["permissions"] == {"checks": "write"}


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


def test_create_opens_the_in_progress_check_on_the_pr_head_and_saves_its_id():
    run, upload = CREATE["steps"]
    assert "head.repo.full_name == github.repository" in CREATE["if"]
    assert "-f name='Global PR Status'" in run["run"]
    assert '-f head_sha="$SHA"' in run["run"]
    assert "-f status=in_progress" in run["run"]
    assert run["env"]["SHA"] == "${{ github.event.pull_request.head.sha }}"
    assert upload["uses"].startswith("actions/upload-artifact@")
    assert upload["with"]["name"] == "global-pr-status"
    assert upload["with"]["path"] == "global-pr-status/check-run-id"


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


def _patches(requests):
    return [request for request in requests if request["key"] == "patch"]


@pytest.mark.parametrize("conclusion", ["success", "skipped", "neutral"])
def test_gate_succeeds_the_status_check_when_everything_passed(run_step, conclusion):
    responses = {
        "runs": [{"workflow_runs": [_run(conclusion=conclusion)]}],
        "checks": [{"check_runs": [_check(conclusion=conclusion)]}],
    }
    result, requests = run_step(GATE, responses)
    assert result.returncode == 0, result.stderr
    assert [request["key"] for request in requests] == ["status_run", "download", "runs", "checks", "patch"]
    assert requests[0]["args"][1].startswith(
        "repos/example/consumer/actions/workflows/create_global_pr_status.yml/runs?head_sha=" + HEAD
    )
    assert requests[1]["args"][:5] == ["run", "download", "9", "--repo", "example/consumer"]
    assert "--paginate" in requests[2]["args"]
    assert "--paginate" in requests[3]["args"]
    assert requests[2]["args"][-1] == f"repos/example/consumer/actions/runs?head_sha={HEAD}&per_page=100"
    assert requests[3]["args"][-1] == f"repos/example/consumer/commits/{HEAD}/check-runs?filter=latest&per_page=100"
    (patch,) = _patches(requests)
    assert f"repos/example/consumer/check-runs/{CHECK_RUN_ID}" in patch["args"]
    assert "status=completed" in patch["args"]
    assert "conclusion=success" in patch["args"]


def test_gate_ignores_its_own_workflow_the_approval_workflow_and_the_status_gate_and_approval_checks(run_step):
    responses = {
        "runs": [
            {
                "workflow_runs": [
                    _run(99, "Custom gate", "in_progress", None),
                    _run(98, "Review when ready", "in_progress", None),
                ]
            }
        ],
        "checks": [
            {
                "check_runs": [
                    _check(index, f"{prefix}{name}", "in_progress", None)
                    for index, (prefix, name) in enumerate(
                        (prefix, name)
                        for prefix in ("", "caller / ", "outer / inner / ")
                        for name in ("all-checks", "auto-approve", "Global PR Status")
                    )
                ]
            }
        ],
    }
    result, requests = run_step(GATE, responses, AUTO_APPROVE_WORKFLOW="Review when ready")
    assert result.returncode == 0, result.stderr
    assert "conclusion=success" in _patches(requests)[0]["args"]


def test_gate_reads_all_pages_and_uses_latest_runs_and_named_check_attempts(run_step):
    responses = {
        "runs": [[{"workflow_runs": [_run(1, conclusion="failure")]}, {"workflow_runs": [_run(2)]}]],
        "checks": [[{"check_runs": [_check(1, conclusion="failure")]}, {"check_runs": [_check(2)]}]],
    }
    result, requests = run_step(GATE, responses)
    assert result.returncode == 0, result.stderr
    assert "conclusion=success" in _patches(requests)[0]["args"]


def test_gate_does_not_hide_a_failed_run_from_a_different_trigger(run_step):
    responses = {
        "runs": [{"workflow_runs": [_run(1, conclusion="failure", event="push"), _run(2)]}],
        "checks": [{"check_runs": []}],
    }
    result, requests = run_step(GATE, responses)
    assert result.returncode == 0, result.stderr
    assert "Tests: failure" in result.stdout
    assert "conclusion=failure" in _patches(requests)[0]["args"]


@pytest.mark.parametrize("source", ["runs", "checks"])
@pytest.mark.parametrize("conclusion", ["failure", "cancelled", "timed_out", "action_required"])
def test_gate_fails_the_status_check_for_unsuccessful_workflows_or_checks(run_step, source, conclusion):
    key, item = ("workflow_runs", _run()) if source == "runs" else ("check_runs", _check())
    item["conclusion"] = conclusion
    result, requests = run_step(GATE, {source: [{key: [item]}]})
    assert result.returncode == 0, result.stderr
    assert "::error::Some checks didn't pass" in result.stdout
    (patch,) = _patches(requests)
    assert "status=completed" in patch["args"]
    assert "conclusion=failure" in patch["args"]


@pytest.mark.parametrize("pending", ["run", "check"])
def test_gate_leaves_the_status_check_open_while_anything_is_still_running(run_step, pending):
    responses = {
        "runs": [{"workflow_runs": [_run()]}],
        "checks": [{"check_runs": [_check()]}],
    }
    if pending == "run":
        responses["runs"] = [{"workflow_runs": [_run(status="in_progress", conclusion=None)]}]
    else:
        responses["checks"] = [{"check_runs": [_check(), _check(2, "later matrix job", "queued", None)]}]
    result, requests = run_step(GATE, responses)
    assert result.returncode == 0, result.stderr
    assert not _patches(requests)
    assert "Still running" in result.stdout


def test_gate_fails_the_status_check_at_once_even_while_other_workflows_run(run_step):
    still_running = {**_run(2, "Terraform", "in_progress", None), "workflow_id": 2}
    responses = {"runs": [{"workflow_runs": [_run(1, conclusion="failure"), still_running]}]}
    # A failure is final whatever is still running: the PR can't pass.
    result, requests = run_step(GATE, responses)
    assert "conclusion=failure" in _patches(requests)[0]["args"]
    assert result.returncode == 0, result.stderr


def test_gate_waits_for_the_status_workflow_to_finish(run_step):
    responses = {"status_run": [{"workflow_runs": [{"id": 9, "status": "in_progress"}]}]}
    result, requests = run_step(GATE, responses)
    assert result.returncode == 0, result.stderr
    assert [request["key"] for request in requests] == ["status_run"]
    assert "::notice::" in result.stdout


@pytest.mark.parametrize("source", ["status_run", "runs", "checks", "patch"])
def test_gate_propagates_api_failures_instead_of_passing_empty_results(run_step, source):
    responses = {"runs": [{"workflow_runs": []}], "checks": [{"check_runs": []}]}
    responses[source] = ["error"]
    result, _ = run_step(GATE, responses)
    assert result.returncode != 0
    assert "GitHub API request failed" in result.stderr


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
