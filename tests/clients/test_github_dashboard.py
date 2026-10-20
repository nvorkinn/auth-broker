import json
import threading
from unittest.mock import Mock

import fakeredis
import pytest
import requests
import responses

from broker.clients import github

ROOT = github.API_URL
REPO = {
    "full_name": "alice/project",
    "owner": {"login": "alice"},
    "archived": False,
    "default_branch": "main",
    "description": "A project",
    "html_url": "https://github.com/alice/project",
}


@pytest.fixture
def api():
    api = github.Dashboard("test-pat")
    yield api
    api.close()


@pytest.mark.parametrize(
    ("tags", "expected"),
    [
        ([], (None, "1.0.0")),
        (["1.19.0"], ("1.19.0", "1.20.0")),
        (["v1.19.4", "v1.20.0-rc.1"], ("v1.20.0-rc.1", "v1.21.0")),
        (["v1.9.9", "v1.19.0", "v1.2.0"], ("v1.19.0", "v1.20.0")),
        (["v1.19.0-rc.1", "v1.19.0"], ("v1.19.0", "v1.20.0")),
        (["other", "2.0.0+build.1"], ("2.0.0+build.1", "2.1.0")),
    ],
)
def test_next_release(tags, expected):
    assert github.next_release([{"tag_name": tag} for tag in tags]) == expected


def test_non_version_tags_are_refused():
    with pytest.raises(github.DashboardError, match="semantic version"):
        github.next_release([{"tag_name": "nightly"}])


@responses.activate
def test_etags_are_revalidated_across_requests_and_scoped_to_the_pat():
    cache = fakeredis.FakeRedis()
    responses.get(f"{ROOT}/user", json={"login": "alice"}, headers={"ETag": '"first"'})
    responses.get(f"{ROOT}/user", status=304)
    responses.get(f"{ROOT}/user", json={"login": "bob"}, headers={"ETag": '"other"'})
    first = github.Dashboard("alice-pat", cache)
    second = github.Dashboard("alice-pat", cache)
    other = github.Dashboard("bob-pat", cache)
    try:
        assert first.get("/user") == {"login": "alice"}
        assert second.get("/user") == {"login": "alice"}
        assert other.get("/user") == {"login": "bob"}
        assert responses.calls[1].request.headers["If-None-Match"] == '"first"'
        assert "If-None-Match" not in responses.calls[2].request.headers
        assert all(0 < cache.ttl(key) <= 3600 for key in cache.scan_iter())
    finally:
        first.close()
        second.close()
        other.close()


@responses.activate
def test_etag_cache_never_hides_an_upstream_error():
    cache = fakeredis.FakeRedis()
    responses.get(f"{ROOT}/user", json={"login": "alice"}, headers={"ETag": '"first"'})
    responses.get(f"{ROOT}/user", status=403)
    api = github.Dashboard("test-pat", cache)
    try:
        api.get("/user")
        with pytest.raises(requests.HTTPError):
            api.get("/user")
    finally:
        api.close()


@responses.activate
def test_discovery_paginates_and_excludes_other_owners_and_archives(api):
    responses.get(f"{ROOT}/user", json={"login": "alice"})
    responses.get(f"{ROOT}/user/repos", json=[REPO] * 100)
    responses.get(
        f"{ROOT}/user/repos",
        json=[REPO | {"archived": True}, REPO | {"owner": {"login": "org"}}],
    )
    assert len(api.repositories()) == 100
    assert "page=2" in responses.calls[-1].request.url
    assert responses.calls[0].request.headers["Authorization"] == "Bearer test-pat"


@responses.activate
@pytest.mark.parametrize("repo", [REPO | {"archived": True}, REPO | {"owner": {"login": "bob"}}])
def test_owned_repository_rechecks_access(api, repo):
    responses.get(f"{ROOT}/user", json={"login": "alice"})
    responses.get(f"{ROOT}/repos/alice/project", json=repo)
    with pytest.raises(github.DashboardError, match="owned"):
        api.owned_repository("alice", "project")


@responses.activate
def test_pull_checks_include_paginated_checks_and_latest_legacy_statuses(api):
    path = "/repos/alice/project"
    responses.get(
        f"{ROOT}{path}/pulls/7",
        json={
            "head": {"sha": "abc", "ref": "feature"},
            "base": {"ref": "main"},
            "user": {"login": "bob"},
            "title": "Change",
            "html_url": "https://github.com/alice/project/pull/7",
            "draft": False,
            "mergeable": False,
            "mergeable_state": "dirty",
        },
    )
    check = {"name": "build", "conclusion": None, "status": "queued", "html_url": None}
    responses.get(f"{ROOT}{path}/commits/abc/check-runs", json={"check_runs": [check] * 100})
    responses.get(f"{ROOT}{path}/commits/abc/check-runs", json={"check_runs": [check | {"name": "test"}]})
    responses.get(
        f"{ROOT}{path}/commits/abc/statuses",
        json=[
            {"context": "deploy", "state": "success", "target_url": None},
            {"context": "deploy", "state": "pending", "target_url": None},
            {"context": "external", "state": "pending", "target_url": None},
        ],
    )
    pr = api.pull(path, 7)
    assert pr["mergeable"] is False
    assert len(pr["checks"]) == 103
    assert pr["refresh"] is True
    assert [check["state"] for check in pr["checks"][-2:]] == ["success", "pending"]


def _runs_by_status(workflows=(), **by_status):
    """Stand in for Dashboard.pages: open PRs, the repo's workflows, and the runs GitHub lists under each status."""

    def pages(path, key=None, **params):
        if path.endswith("/pulls"):
            return [{"number": 3}]
        if path.endswith("/actions/workflows"):
            return list(workflows)
        return by_status.get(params["status"], [])

    return Mock(side_effect=pages)


def test_project_reads_every_unfinished_status_and_deduplicates(api):
    api.pages = _runs_by_status(
        waiting=[{"id": 2, "name": "Release packages", "status": "waiting"}],
        queued=[{"id": 1, "name": "Release", "status": "queued"}],
        in_progress=[{"id": 1, "name": "Release", "status": "in_progress"}],
    )
    api.pull = Mock(return_value={"refresh": False})
    api.workflow = Mock(side_effect=lambda _repo, run: run)
    result = api.project(REPO)
    assert sorted(run["id"] for run in result["workflows"]) == [1, 2]
    assert result["active"] is True
    # Lifecycle order: a run approved mid-sweep leaves "waiting" for a status that is queried later.
    assert [call.kwargs["status"] for call in api.pages.call_args_list if "status" in call.kwargs] == [
        "requested",
        "pending",
        "waiting",
        "queued",
        "in_progress",
    ]


def test_project_reads_only_the_section_asked_for(api):
    api.pages = _runs_by_status(queued=[{"id": 1, "name": "Release", "status": "queued"}])
    api.pull = Mock(return_value={"refresh": False})
    api.workflow = Mock(side_effect=lambda _repo, run: run)
    pulls = api.project(REPO, section="pulls")
    assert pulls == {"pulls": [{"refresh": False}], "active": False}
    api.workflow.assert_not_called()
    assert all(call.args[0].endswith("/pulls") for call in api.pages.call_args_list)
    api.pages.reset_mock()
    api.pull.reset_mock()
    workflows = api.project(REPO, section="workflows")
    assert workflows == {"workflows": [{"id": 1, "name": "Release", "status": "queued"}], "active": True}
    api.pull.assert_not_called()
    assert not any(call.args[0].endswith("/pulls") for call in api.pages.call_args_list)


def test_project_reads_pull_requests_concurrently_and_keeps_their_order(api):
    started = threading.Barrier(3, timeout=5)

    def pull(_path, number):
        started.wait()  # Returns only if every PR is being read at the same time.
        return {"number": number, "refresh": False}

    api.pages = Mock(return_value=[{"number": 3}, {"number": 1}, {"number": 2}])
    api.pull = Mock(side_effect=pull)
    assert [pr["number"] for pr in api.project(REPO, section="pulls")["pulls"]] == [3, 1, 2]


def test_a_failed_part_fails_the_project_read(api):
    api.pages = _runs_by_status()
    api.pull = Mock(side_effect=requests.ConnectionError("down"))
    with pytest.raises(requests.ConnectionError):
        api.project(REPO)


@responses.activate
def test_project_rechecks_known_runs_that_the_status_lists_missed(api):
    path = f"{ROOT}/repos/alice/project"
    api.pages = _runs_by_status(in_progress=[{"id": 1, "name": "Release", "status": "in_progress"}])
    api.pull = Mock(return_value={"refresh": False})
    api.workflow = Mock(side_effect=lambda _repo, run: run)
    responses.get(f"{path}/actions/runs/7", json={"id": 7, "name": "Release", "status": "queued"})
    responses.get(f"{path}/actions/runs/8", json={"id": 8, "name": "Release", "status": "completed"})
    responses.get(f"{path}/actions/runs/9", status=404)
    result = api.project(REPO, [1, 7, 8, 9])
    # Run 8 finished since the last look, so it stays as the latest finished release.
    assert [run["id"] for run in result["workflows"]] == [1, 7, 8]
    assert result["active"] is True
    assert sorted(call.request.url.split("/")[-1] for call in responses.calls) == ["7", "8", "9"]


def test_the_latest_finished_release_is_kept_after_the_ongoing_ones(api):
    api.pages = _runs_by_status(
        workflows=[{"id": 1, "name": "Release"}, {"id": 2, "name": "Tests"}, {"id": 3, "name": "Release packages"}],
        in_progress=[{"id": 20, "name": "Tests", "status": "in_progress"}],
    )
    latest = {1: {"id": 12, "name": "Release", "status": "completed"}, 3: {"id": 15, "name": "Release packages"}}
    api.get = Mock(side_effect=lambda path, **_: {"workflow_runs": [latest[int(path.split("/")[-2])]]})
    api.pull = Mock(return_value={"refresh": False})
    api.workflow = Mock(side_effect=lambda _repo, run: run)
    result = api.project(REPO)
    assert [run["id"] for run in result["workflows"]] == [15]
    assert result["active"] is True
    assert sorted(call.args[0].split("/")[-2] for call in api.get.call_args_list) == ["1", "3"]
    assert all(call.kwargs == {"status": "completed", "per_page": 1} for call in api.get.call_args_list)


@responses.activate
def test_a_release_rechecked_by_id_that_just_finished_beats_the_lagging_workflow_lists(api):
    api.pages = _runs_by_status(workflows=[{"id": 1, "name": "Release"}])
    responses.get(
        f"{ROOT}/repos/alice/project/actions/runs/13", json={"id": 13, "name": "Release", "status": "completed"}
    )
    responses.get(
        f"{ROOT}/repos/alice/project/actions/workflows/1/runs",
        json={"workflow_runs": [{"id": 12, "name": "Release", "status": "completed"}]},
    )
    api.workflow = Mock(side_effect=lambda _repo, run: run)
    assert [run["id"] for run in api.project(REPO, [13], "workflows")["workflows"]] == [13]


@responses.activate
def test_a_finished_workflow_has_its_outcome_and_no_approval_lookup(api):
    path = f"{ROOT}/repos/alice/project"
    responses.get(f"{path}/actions/runs/4/attempts/1/jobs", json={"jobs": []})
    responses.get(f"{path}/contents/.github/workflows/release.yml", body="jobs:\n  build: {}\n")
    run = {
        "id": 4,
        "run_attempt": 1,
        "path": ".github/workflows/release.yml",
        "head_sha": "abc",
        "name": "Release",
        "run_number": 9,
        "head_branch": "v1.2.0",
        "status": "completed",
        "conclusion": "failure",
        "updated_at": "2026-10-20T02:00:00Z",
        "html_url": "https://github.com/run/4",
    }
    result = api.workflow(REPO, run)
    assert (result["conclusion"], result["finished_at"]) == ("failure", "2026-10-20T02:00:00Z")
    assert result["pending_deployments"] == []
    assert not any("pending_deployments" in call.request.url for call in responses.calls)


@responses.activate
@pytest.mark.parametrize(
    ("status", "body", "found"),
    [(200, {"id": 4, "name": "Release"}, True), (200, {"id": 4, "name": "Tests"}, False), (404, {}, False)],
)
def test_release_run_reads_one_release_and_nothing_else(api, status, body, found):
    responses.get(f"{ROOT}/repos/alice/project/actions/runs/4", json=body, status=status)
    api.workflow = Mock(side_effect=lambda _repo, run: run)
    assert api.release_run(REPO, 4) == (body if found else None)


@pytest.mark.parametrize(
    ("name", "shown"),
    [
        ("Release", True),
        ("release packages", True),
        ("Prerelease", True),
        ("Tests", False),
        ("CodeQL", False),
        ("Terraform", False),
        ("Deploy", False),
    ],
)
def test_only_release_workflows_are_drawn_but_other_ci_still_keeps_fast_polling(api, name, shown):
    run = {"id": 7, "name": name, "status": "waiting"}
    api.pages = _runs_by_status(waiting=[run])
    api.pull = Mock(return_value={"refresh": False})
    api.workflow = Mock(return_value=run)
    result = api.project(REPO)
    assert result["workflows"] == ([run] if shown else [])
    assert result["active"] is True
    assert api.workflow.call_count == int(shown)


@responses.activate
def test_waiting_workflow_includes_unstarted_nodes_edges_and_job_steps(api):
    path = f"{ROOT}/repos/alice/project"
    responses.get(
        f"{path}/actions/runs/4/attempts/2/jobs",
        json={
            "jobs": [
                {
                    "name": "build",
                    "status": "completed",
                    "conclusion": "success",
                    "html_url": "https://github.com/jobs/1",
                    "steps": [{"name": "Compile", "status": "completed", "conclusion": "success"}],
                }
            ]
        },
    )
    responses.get(
        f"{path}/contents/.github/workflows/release.yml", body="jobs:\n  build: {}\n  deploy:\n    needs: build\n"
    )
    responses.get(
        f"{path}/actions/runs/4/pending_deployments",
        json=[
            {"environment": {"id": 10, "name": "production"}, "current_user_can_approve": True, "wait_timer": 0},
        ],
    )
    result = api.workflow(
        REPO,
        {
            "id": 4,
            "run_attempt": 2,
            "path": ".github/workflows/release.yml@main",
            "head_sha": "abc",
            "name": "Release",
            "run_number": 9,
            "head_branch": "main",
            "status": "waiting",
            "html_url": "https://github.com/run/4",
        },
    )
    assert result["status"] == "waiting"
    assert [(node["state"], node["needs"]) for node in result["nodes"]] == [("done", []), ("queued", ["build"])]
    assert result["jobs"][0]["steps"][0]["name"] == "Compile"
    assert result["pending_deployments"] == [{"id": 10, "name": "production", "can_approve": True, "wait_timer": 0}]
    assert any(
        "/contents/.github/workflows/release.yml" in call.request.url and "ref=abc" in call.request.url
        for call in responses.calls
    )


@responses.activate
def test_approve_rechecks_run_and_only_approves_selected_environment(api):
    path = f"{ROOT}/repos/alice/project/actions/runs/4"
    responses.get(path, json={"name": "Release", "status": "waiting"})
    responses.get(
        f"{path}/pending_deployments",
        json=[
            {"environment": {"id": 10, "name": "production"}, "current_user_can_approve": True, "wait_timer": 0},
            {"environment": {"id": 11, "name": "staging"}, "current_user_can_approve": True, "wait_timer": 0},
        ],
    )
    responses.post(f"{path}/pending_deployments", json=[{"id": 7}])
    api.approve_deployment(REPO, 4, 10)
    payload = json.loads(responses.calls[-1].request.body)
    assert payload["environment_ids"] == [10]
    assert payload["state"] == "approved"


@responses.activate
@pytest.mark.parametrize(
    ("name", "status", "pending", "message"),
    [
        ("Tests", "waiting", [], "unfinished release"),
        ("Release", "completed", [], "unfinished release"),
        ("Release", "waiting", [], "no longer waiting"),
        (
            "Release",
            "waiting",
            [
                {"environment": {"id": 10, "name": "production"}, "current_user_can_approve": False, "wait_timer": 0},
            ],
            "cannot approve",
        ),
    ],
)
def test_approval_refuses_non_release_completed_missing_or_unauthorized_gates(api, name, status, pending, message):
    path = f"{ROOT}/repos/alice/project/actions/runs/4"
    responses.get(path, json={"name": name, "status": status})
    if name == "Release" and status != "completed":
        responses.get(f"{path}/pending_deployments", json=pending)
    with pytest.raises(github.DashboardError, match=message):
        api.approve_deployment(REPO, 4, 10)
    assert all(call.request.method == "GET" for call in responses.calls)


def test_workflow_parse_diagnostics_are_logged_not_exposed(api, caplog):
    api.pages = Mock(return_value=[])
    api.read = Mock(return_value="jobs: [\n  private-workflow-content")
    run = {
        "id": 4,
        "run_attempt": 2,
        "path": ".github/workflows/release.yml",
        "head_sha": "abc",
        "name": "Release",
        "status": "in_progress",
    }
    with pytest.raises(github.DashboardError) as caught:
        api.workflow(REPO, run)
    assert caught.value.public_message == "The workflow file could not be parsed. Check the server logs for details."
    assert isinstance(caught.value.__cause__, ValueError)
    assert "private-workflow-content" in caplog.text
    assert "private-workflow-content" not in caught.value.public_message


@responses.activate
def test_publish_includes_prereleases_ignores_drafts_and_generates_notes(api):
    path = f"{ROOT}/repos/alice/project"
    responses.get(
        f"{path}/releases",
        json=[
            {"tag_name": "v1.19.0", "draft": False},
            {"tag_name": "v1.20.0-rc.1", "draft": False},
            {"tag_name": "v9.0.0", "draft": True},
        ],
    )
    responses.get(f"{path}/git/ref/tags/v1.21.0", status=404)
    responses.post(f"{path}/releases/generate-notes", json={"name": "v1.21.0", "body": "Generated changes"})
    responses.post(f"{path}/releases", json={"tag_name": "v1.21.0"}, status=201)
    assert api.publish_release(REPO)["tag_name"] == "v1.21.0"
    notes = json.loads(responses.calls[-2].request.body)
    release = json.loads(responses.calls[-1].request.body)
    assert notes == {"tag_name": "v1.21.0", "target_commitish": "main", "previous_tag_name": "v1.20.0-rc.1"}
    assert release["body"] == "Generated changes"
    assert release["draft"] is False
    assert release["prerelease"] is False


@responses.activate
def test_existing_tag_prevents_publication(api):
    path = f"{ROOT}/repos/alice/project"
    responses.get(f"{path}/releases", json=[])
    responses.get(f"{path}/git/ref/tags/1.0.0", json={"ref": "refs/tags/1.0.0"})
    with pytest.raises(github.DashboardError, match="already exists"):
        api.publish_release(REPO)
    assert not any(call.request.method == "POST" for call in responses.calls)


@responses.activate
def test_note_generation_failure_never_publishes(api):
    path = f"{ROOT}/repos/alice/project"
    responses.get(f"{path}/releases", json=[])
    responses.get(f"{path}/git/ref/tags/1.0.0", status=404)
    responses.post(f"{path}/releases/generate-notes", status=403)
    with pytest.raises(requests.HTTPError):
        api.publish_release(REPO)
    assert len(responses.calls) == 3
