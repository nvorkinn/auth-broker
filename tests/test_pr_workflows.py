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
    for name in ("all-checks.yml", "auto_approve.yml")
}
GATE = WORKFLOWS["all-checks.yml"]["jobs"]["all-checks"]["steps"][0]
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
            if any("/actions/runs?" in arg for arg in args):
                key = "runs"
            elif any("/check-runs?" in arg for arg in args):
                key = "checks"
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
                responses
                or {
                    "runs": [{"workflow_runs": []}],
                    "checks": [{"check_runs": []}],
                    "pr": [{"head": {"sha": HEAD}}],
                    "reviews": [{"state": "APPROVED"}],
                }
            ),
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


def test_workflows_are_callable_and_retain_their_pr_triggers_and_permissions():
    for workflow in WORKFLOWS.values():
        assert {"pull_request", "workflow_call"} <= workflow["on"].keys()
        assert workflow["permissions"]["actions"] == "read"
        assert workflow["permissions"]["checks"] == "read"
    assert WORKFLOWS["auto_approve.yml"]["permissions"]["contents"] == "read"
    assert WORKFLOWS["auto_approve.yml"]["permissions"]["pull-requests"] == "write"
    assert WORKFLOWS["auto_approve.yml"]["on"]["pull_request"]["types"] == [
        "opened",
        "synchronize",
        "reopened",
        "ready_for_review",
    ]
    contract = WORKFLOWS["all-checks.yml"]["on"]["workflow_call"]
    assert contract["inputs"]["auto-approve-workflow-name"]["default"] == "Auto-approve PRs"
    assert contract["inputs"]["auto-approve-workflow-name"]["required"] == "false"


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


@pytest.mark.parametrize("conclusion", ["success", "skipped", "neutral"])
def test_gate_accepts_passing_conclusions_after_two_settled_polls(run_step, conclusion):
    responses = {
        "runs": [{"workflow_runs": [_run(conclusion=conclusion)]}],
        "checks": [{"check_runs": [_check(conclusion=conclusion)]}],
    }
    result, requests = run_step(GATE, responses)
    assert result.returncode == 0, result.stderr
    assert [request["key"] for request in requests] == ["runs", "checks"] * 2
    assert all("--paginate" in request["args"] for request in requests)
    assert requests[0]["args"][-1] == f"repos/example/consumer/actions/runs?head_sha={HEAD}&per_page=100"
    assert requests[1]["args"][-1] == f"repos/example/consumer/commits/{HEAD}/check-runs?filter=latest&per_page=100"


def test_gate_ignores_current_run_custom_caller_and_nested_gate_and_approval_checks(run_step):
    responses = {
        "runs": [
            {
                "workflow_runs": [
                    _run(100, "Current run", "in_progress", None),
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
                        for name in ("all-checks", "auto-approve")
                    )
                ]
            }
        ],
    }
    result, requests = run_step(GATE, responses, AUTO_APPROVE_WORKFLOW="Review when ready")
    assert result.returncode == 0, result.stderr
    assert len(requests) == 4


def test_gate_reads_all_pages_and_uses_latest_runs_and_named_check_attempts(run_step):
    responses = {
        "runs": [[{"workflow_runs": [_run(1, conclusion="failure")]}, {"workflow_runs": [_run(2)]}]],
        "checks": [[{"check_runs": [_check(1, conclusion="failure")]}, {"check_runs": [_check(2)]}]],
    }
    result, requests = run_step(GATE, responses)
    assert result.returncode == 0, result.stderr
    assert len(requests) == 4


def test_gate_does_not_hide_a_failed_run_from_a_different_trigger(run_step):
    responses = {
        "runs": [{"workflow_runs": [_run(1, conclusion="failure", event="push"), _run(2)]}],
        "checks": [{"check_runs": []}],
    }
    result, _ = run_step(GATE, responses)
    assert result.returncode == 1
    assert "Tests: failure" in result.stdout


@pytest.mark.parametrize("source", ["runs", "checks"])
@pytest.mark.parametrize("conclusion", ["failure", "cancelled", "timed_out", "action_required"])
def test_gate_rejects_unsuccessful_workflows_or_checks(run_step, source, conclusion):
    responses = {
        "runs": [{"workflow_runs": [_run()]}],
        "checks": [{"check_runs": [_check()]}],
    }
    key, item = ("workflow_runs", _run()) if source == "runs" else ("check_runs", _check())
    item["conclusion"] = conclusion
    responses[source] = [{key: [item]}]
    result, _ = run_step(GATE, responses)
    assert result.returncode == 1
    assert "::error::Some checks didn't pass" in result.stdout


def test_gate_resets_settlement_for_later_matrix_jobs_and_waits_for_workflow_completion(run_step):
    responses = {
        "runs": [
            {"workflow_runs": [_run()]},
            {"workflow_runs": [_run(status="in_progress", conclusion=None)]},
            {"workflow_runs": [_run()]},
        ],
        "checks": [
            {"check_runs": [_check()]},
            {"check_runs": [_check(), _check(2, "later matrix job", "queued", None)]},
            {"check_runs": [_check(), _check(2, "later matrix job")]},
        ],
    }
    result, requests = run_step(GATE, responses)
    assert result.returncode == 0, result.stderr
    assert len(requests) == 8


@pytest.mark.parametrize("source", ["runs", "checks"])
def test_gate_propagates_api_failures_instead_of_passing_empty_results(run_step, source):
    responses = {"runs": [{"workflow_runs": []}], "checks": [{"check_runs": []}]}
    responses[source] = ["error"]
    result, _ = run_step(GATE, responses)
    assert result.returncode != 0
    assert "GitHub API request failed" in result.stderr


def test_gate_rejects_callers_without_a_pr_head(run_step):
    result, requests = run_step(GATE, SHA="")
    assert result.returncode == 1
    assert "::error::All checks must be called from a pull_request workflow" in result.stdout
    assert not requests


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
