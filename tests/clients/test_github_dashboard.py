import json
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


def test_project_reads_every_unfinished_status_and_deduplicates(api):
    api.pages = Mock(
        side_effect=[
            [{"number": 3}],
            [],
            [],
            [{"id": 2, "name": "Release packages", "status": "waiting"}],
            [{"id": 1, "name": "Release", "status": "queued"}],
            [{"id": 1, "name": "Release", "status": "in_progress"}],
        ]
    )
    api.pull = Mock(return_value={"refresh": False})
    api.workflow = Mock(side_effect=lambda _repo, run: run)
    result = api.project(REPO)
    assert sorted(run["id"] for run in result["workflows"]) == [1, 2]
    assert result["active"] is True
    # Lifecycle order: a run approved mid-sweep leaves "waiting" for a status that is queried later.
    assert [call.kwargs.get("status") for call in api.pages.call_args_list[1:]] == [
        "requested",
        "pending",
        "waiting",
        "queued",
        "in_progress",
    ]


@responses.activate
def test_project_rechecks_known_runs_that_the_status_lists_missed(api):
    path = f"{ROOT}/repos/alice/project"
    api.pages = Mock(side_effect=[[], [], [], [], [{"id": 1, "name": "Release", "status": "in_progress"}], []])
    api.workflow = Mock(side_effect=lambda _repo, run: run)
    responses.get(f"{path}/actions/runs/7", json={"id": 7, "name": "Release", "status": "queued"})
    responses.get(f"{path}/actions/runs/8", json={"id": 8, "name": "Release", "status": "completed"})
    responses.get(f"{path}/actions/runs/9", status=404)
    result = api.project(REPO, [1, 7, 8, 9])
    assert sorted(run["id"] for run in result["workflows"]) == [1, 7]
    assert result["active"] is True
    assert [call.request.url.split("/")[-1] for call in responses.calls] == ["7", "8", "9"]


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
    api.pages = Mock(side_effect=[[], [], [], [run], [], []])
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
    assert "ref=abc" in responses.calls[-2].request.url


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
