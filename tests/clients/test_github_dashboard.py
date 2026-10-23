import json
from unittest.mock import Mock

import fakeredis
import pytest
import requests
import responses

from broker.clients import github

ROOT = github.API_URL


def _url(call):
    """Return the URL a call to the API went to, which a sent request always has."""
    assert call.request.url is not None
    return call.request.url


def _json_body(call):
    """Return the JSON a call to the API sent."""
    assert isinstance(call.request.body, str | bytes)
    return json.loads(call.request.body)


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
    assert "page=2" in _url(responses.calls[-1])
    assert responses.calls[0].request.headers["Authorization"] == "Bearer test-pat"


@responses.activate
@pytest.mark.parametrize("repo", [REPO | {"archived": True}, REPO | {"owner": {"login": "bob"}}])
def test_owned_repository_rechecks_access(api, repo):
    responses.get(f"{ROOT}/user", json={"login": "alice"})
    responses.get(f"{ROOT}/repos/alice/project", json=repo)
    with pytest.raises(github.DashboardError, match="owned"):
        api.owned_repository("alice", "project")


@pytest.mark.parametrize(
    ("body", "session"),
    [
        (None, None),
        ("No link here.", None),
        ("See https://claude.ai/code/project/chan_01X?thread=cmsg_01Y", None),
        ("https://claude.ai/code/session_01Abc?x=1", "session_01Abc"),
        ("Written by https://claude.ai/code/session_01Abc.", "session_01Abc"),
        ("https://evil.example/claude.ai/code/session_01Abc", None),
    ],
)
def test_a_pr_links_its_claude_session_only_from_a_claude_ai_url(body, session):
    assert github._claude_session(body) == session


def test_pull_files_keep_patches_and_stop_after_the_last_page(api):
    page = [
        {"filename": f"f{index}", "status": "modified", "additions": 1, "deletions": 0, "patch": "@@"}
        for index in range(100)
    ]
    large = {
        "filename": "big.json",
        "status": "added",
        "additions": 9000,
        "deletions": 0,
        "blob_url": "https://github.com/b",
    }
    api.read = Mock(side_effect=[json.dumps(page), json.dumps([large])])
    found = api.pull_files("/repos/alice/project", 7)
    assert len(found["files"]) == 101
    assert found["truncated"] is False
    assert found["files"][-1] == {
        "filename": "big.json",
        "previous_filename": None,
        "status": "added",
        "additions": 9000,
        "deletions": 0,
        "patch": None,
        "url": "https://github.com/b",
    }
    assert api.read.call_count == 2


def test_pull_files_are_cut_at_the_shown_maximum(api):
    page = [{"filename": "f", "status": "modified", "additions": 1, "deletions": 0} for _ in range(100)]
    api.read = Mock(return_value=json.dumps(page))
    found = api.pull_files("/repos/alice/project", 7)
    assert len(found["files"]) == github.MAX_FILES
    assert found["truncated"] is True
    assert api.read.call_count == 4


def test_a_closed_pull_request_reads_as_none_and_is_left_out(api):
    api.get = Mock(return_value={"state": "closed"})
    assert api.pull("/repos/alice/project", 7) is None


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
            "created_at": "2026-10-19T08:00:00Z",
            "body": "Change.\n\nhttps://claude.ai/code/session_01Old\n\nhttps://claude.ai/code/session_01Fwmw2krp",
            "state": "open",
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
    assert pr["created_at"] == "2026-10-19T08:00:00Z"
    assert pr["session"] == "session_01Fwmw2krp"
    assert pr["sha"] == "abc"
    assert len(pr["checks"]) == 103
    assert pr["refresh"] is True
    assert [check["state"] for check in pr["checks"][-2:]] == ["success", "pending"]


def _release_runs(workflows=({"id": 1, "name": "Release"},), **runs):
    """Stand in for Dashboard.pages and .get: open PRs, the repo's workflows, and each workflow's latest runs."""

    def pages(path, key=None, **params):
        return list(workflows)

    def get(path, **params):
        return {"workflow_runs": runs.get(f"w{path.split('/')[-2]}", [])}

    return Mock(side_effect=pages), Mock(side_effect=get)


def test_release_runs_are_read_once_per_release_workflow(api):
    api.pages, api.get = _release_runs(
        workflows=[{"id": 1, "name": "Release"}, {"id": 2, "name": "Tests"}, {"id": 3, "name": "Release packages"}],
        w1=[{"id": 11, "name": "Release", "status": "queued"}],
        w3=[{"id": 31, "name": "Release packages", "status": "waiting"}],
    )
    api.pulls = Mock(return_value=[{"refresh": False}])
    api.workflow = Mock(side_effect=lambda _repo, run: run)
    result = api.project(REPO)
    assert [run["id"] for run in result["workflows"]] == [11, 31]
    assert sorted(call.args[0].split("/")[-2] for call in api.get.call_args_list) == ["1", "3"]
    assert all(call.kwargs == {"per_page": github.RECENT_RELEASES} for call in api.get.call_args_list)


def test_unfinished_releases_come_first_then_the_latest_finished_one(api):
    api.pages, api.get = _release_runs(
        workflows=[{"id": 1, "name": "Release"}, {"id": 3, "name": "Release packages"}],
        w1=[
            {"id": 14, "name": "Release", "status": "in_progress"},
            {"id": 13, "name": "Release", "status": "completed"},
            {"id": 12, "name": "Release", "status": "waiting"},
            {"id": 11, "name": "Release", "status": "completed"},
        ],
        w3=[{"id": 15, "name": "Release packages", "status": "completed"}],
    )
    api.workflow = Mock(side_effect=lambda _repo, run: run)
    # Run 12 still waits behind the newer 14, so both show; of the finished ones only the latest, 15, stays.
    assert [run["id"] for run in api.project(REPO, "workflows")["workflows"]] == [14, 12, 15]


def test_project_reads_only_the_section_asked_for(api):
    api.pages, api.get = _release_runs(w1=[{"id": 1, "name": "Release", "status": "queued"}])
    api.pulls = Mock(return_value=[{"refresh": False}])
    api.workflow = Mock(side_effect=lambda _repo, run: run)
    pulls = api.project(REPO, section="pulls")
    assert pulls == {"pulls": [{"refresh": False}]}
    api.pulls.assert_called_once_with("/repos/alice/project")
    api.workflow.assert_not_called()
    api.get.assert_not_called()
    api.pages.assert_not_called()
    api.pulls.reset_mock()
    workflows = api.project(REPO, section="workflows")
    assert workflows == {"workflows": [{"id": 1, "name": "Release", "status": "queued"}]}
    api.pulls.assert_not_called()


def _page(nodes, cursor=None):
    """Return a GraphQL connection page: its nodes, and the cursor of the next page if there is one."""
    return {"pageInfo": {"hasNextPage": cursor is not None, "endCursor": cursor}, "nodes": nodes}


def _graphql_pr(number, contexts=None, **fields):
    """Return an open PR as PULLS_QUERY reads it, its head's checks in `contexts` (None if it has none)."""
    rollup = None if contexts is None else {"contexts": contexts}
    return {
        "number": number,
        "title": f"Change {number}",
        "url": f"https://github.com/alice/project/pull/{number}",
        "author": {"login": "bob"},
        "isDraft": False,
        "headRefName": "feature",
        "baseRefName": "main",
        "mergeable": "MERGEABLE",
        "mergeStateStatus": "CLEAN",
        "createdAt": "2026-10-19T08:00:00Z",
        "body": None,
        "headRefOid": f"sha{number}",
        "autoMergeRequest": None,
        "commits": {"nodes": [{"commit": {"statusCheckRollup": rollup}}]},
    } | fields


def _pulls_page(nodes, cursor=None):
    return {"data": {"repository": {"pullRequests": _page(nodes, cursor)}}}


def _run(run_id, name, status="COMPLETED", conclusion="SUCCESS", suite=1):
    return {
        "__typename": "CheckRun",
        "databaseId": run_id,
        "name": name,
        "status": status,
        "conclusion": conclusion,
        "url": f"https://github.com/alice/project/runs/{run_id}",
        "checkSuite": {"databaseId": suite},
    }


def _status(context, state, created_at="2026-10-19T08:00:00Z"):
    return {
        "__typename": "StatusContext",
        "context": context,
        "state": state,
        "targetUrl": None,
        "createdAt": created_at,
    }


@responses.activate
def test_open_pulls_are_read_in_one_query_shaped_like_a_single_pull(api):
    body = "Change.\n\nhttps://claude.ai/code/session_01Old\n\nhttps://claude.ai/code/session_01Fwmw2krp"
    pr = _graphql_pr(
        7,
        _page([_run(1, "build", "IN_PROGRESS", None), _status("deploy", "SUCCESS")]),
        mergeable="CONFLICTING",
        mergeStateStatus="DIRTY",
        body=body,
        isDraft=True,
        autoMergeRequest={"enabledAt": "2026-10-19T09:00:00Z"},
    )
    query = responses.post(f"{ROOT}/graphql", json=_pulls_page([pr]))
    assert api.pulls("/repos/alice/project") == [
        {
            "number": 7,
            "title": "Change 7",
            "url": "https://github.com/alice/project/pull/7",
            "author": "bob",
            "draft": True,
            "head": "feature",
            "base": "main",
            "mergeable": False,
            "merge_state": "dirty",
            "created_at": "2026-10-19T08:00:00Z",
            "session": "session_01Fwmw2krp",
            "sha": "sha7",
            "auto_merge": True,
            "checks": [
                {
                    "name": "build",
                    "state": "in_progress",
                    "pending": True,
                    "url": "https://github.com/alice/project/runs/1",
                },
                {"name": "deploy", "state": "success", "pending": False, "url": None},
            ],
            "refresh": True,
        }
    ]
    assert query.call_count == 1
    sent = _json_body(query.calls[0])
    assert sent["variables"] == {"owner": "alice", "name": "project", "first": github.PAGE_SIZE, "after": None}
    assert "pullRequests(states: OPEN" in sent["query"]
    assert "fragment checks on StatusCheckRollupContextConnection" in sent["query"]


@pytest.mark.parametrize(
    ("mergeable", "expected", "refresh"),
    [("MERGEABLE", True, False), ("CONFLICTING", False, False), ("UNKNOWN", None, True)],
)
def test_graphql_mergeability_reads_as_rests(api, mergeable, expected, refresh):
    api.graphql = Mock(return_value=_pulls_page([_graphql_pr(1, mergeable=mergeable)])["data"])
    [pr] = api.pulls("/repos/alice/project")
    assert (pr["mergeable"], pr["checks"], pr["refresh"]) == (expected, [], refresh)


def test_a_deleted_authors_pull_reads_as_ghosts(api):
    api.graphql = Mock(return_value=_pulls_page([_graphql_pr(1, author=None)])["data"])
    assert api.pulls("/repos/alice/project")[0]["author"] == "ghost"


@responses.activate
def test_open_pulls_and_their_checks_are_paginated_in_order(api):
    first = _graphql_pr(3, _page([_run(1, "build")], cursor="checks-2"))
    query = responses.post(f"{ROOT}/graphql", json=_pulls_page([first, _graphql_pr(2)], cursor="pulls-2"))
    responses.post(f"{ROOT}/graphql", json=_pulls_page([_graphql_pr(1)]))
    more = {"repository": {"object": {"statusCheckRollup": {"contexts": _page([_run(2, "test", "QUEUED", None)])}}}}
    responses.post(f"{ROOT}/graphql", json={"data": more})
    pulls = api.pulls("/repos/alice/project")
    assert [pr["number"] for pr in pulls] == [3, 2, 1]
    assert [(check["name"], check["state"], check["pending"]) for check in pulls[0]["checks"]] == [
        ("build", "success", False),
        ("test", "queued", True),
    ]
    assert pulls[0]["refresh"] is True
    sent = [_json_body(call)["variables"] for call in responses.calls]
    assert [variables.get("after") for variables in sent] == [None, "pulls-2", "checks-2"]
    assert sent[2]["sha"] == "sha3"
    assert query.call_count == 1


def test_a_head_that_moved_on_mid_read_keeps_the_checks_already_read(api):
    pr = _graphql_pr(3, _page([_run(1, "build")], cursor="checks-2"))
    api.graphql = Mock(side_effect=[_pulls_page([pr])["data"], {"repository": {"object": None}}])
    assert [check["name"] for check in api.pulls("/repos/alice/project")[0]["checks"]] == ["build"]


def test_only_the_latest_run_of_each_check_and_status_of_each_context_is_kept():
    contexts = [
        _run(5, "build", conclusion="SUCCESS"),
        _run(3, "build", conclusion="FAILURE"),
        # Another workflow's job of the same name is its own check.
        _run(4, "build", conclusion="CANCELLED", suite=2),
        _run(6, "lint", "WAITING", None),
        _status("deploy", "SUCCESS", "2026-10-19T09:00:00Z"),
        _status("deploy", "PENDING", "2026-10-19T08:00:00Z"),
        _status("external", "EXPECTED"),
        _status("legacy", "ERROR"),
    ]
    assert [(check["name"], check["state"], check["pending"]) for check in github._rollup_checks(contexts)] == [
        ("build", "success", False),
        ("build", "cancelled", False),
        ("lint", "waiting", True),
        ("deploy", "success", False),
        ("external", "pending", True),
        ("legacy", "error", False),
    ]


@responses.activate
def test_a_graphql_error_is_refused_with_githubs_message(api):
    responses.post(f"{ROOT}/graphql", json={"data": None, "errors": [{"type": "NOT_FOUND", "message": "No repo"}]})
    with pytest.raises(github.DashboardError, match="GitHub refused: No repo"):
        api.pulls("/repos/alice/project")


@responses.activate
def test_graphqls_rate_limit_waits_until_its_reset(api):
    responses.post(
        f"{ROOT}/graphql",
        json={"errors": [{"type": "RATE_LIMITED", "message": "API rate limit exceeded"}]},
        headers={"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": "1300"},
    )
    with pytest.raises(requests.HTTPError) as error:
        api.pulls("/repos/alice/project")
    assert github.rate_limit_wait(error.value, now=1000) == 300


@responses.activate
def test_a_graphql_secondary_limit_keeps_its_retry_after(api):
    responses.post(f"{ROOT}/graphql", status=403, headers={"Retry-After": "30"}, json={"message": "secondary"})
    with pytest.raises(requests.HTTPError) as error:
        api.pulls("/repos/alice/project")
    assert github.rate_limit_wait(error.value) == 30


def test_a_failed_part_fails_the_project_read(api):
    api.pages, api.get = _release_runs()
    api.pulls = Mock(side_effect=requests.ConnectionError("down"))
    with pytest.raises(requests.ConnectionError):
        api.project(REPO)


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
    assert not any("pending_deployments" in _url(call) for call in responses.calls)


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
def test_only_release_workflows_are_drawn(api, name, shown):
    run = {"id": 7, "name": name, "status": "waiting"}
    api.pages, api.get = _release_runs(workflows=[{"id": 1, "name": name}], w1=[run])
    api.pulls = Mock(return_value=[])
    api.workflow = Mock(return_value=run)
    result = api.project(REPO)
    assert result["workflows"] == ([run] if shown else [])
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
        "/contents/.github/workflows/release.yml" in _url(call) and "ref=abc" in _url(call) for call in responses.calls
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
    payload = _json_body(responses.calls[-1])
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
    notes = _json_body(responses.calls[-2])
    release = _json_body(responses.calls[-1])
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


MERGE_REPO = {"full_name": "alice/project"}
OPEN_PR = {
    "state": "open",
    "draft": False,
    "auto_merge": None,
    "node_id": "PR_1",
    "head": {"sha": "abc"},
    "base": {"ref": "main"},
}


def graphql_reply(errors=None):
    response = Mock()
    response.json.return_value = {"errors": errors} if errors else {"data": {}}
    return response


def test_auto_merge_is_turned_on_with_the_repositorys_merge_method(api):
    api.get = Mock(return_value=OPEN_PR)
    api.post = Mock(return_value=graphql_reply())
    assert api.set_auto_merge(MERGE_REPO | {"allow_merge_commit": False}, 7, enabled=True) == {
        "auto_merge": True,
        "merged": False,
    }
    path, payload = api.post.call_args.args
    assert path == "/graphql"
    assert "enablePullRequestAutoMerge" in payload["query"]
    assert payload["variables"] == {"id": "PR_1", "method": "SQUASH"}


def test_auto_merge_is_turned_off(api):
    api.get = Mock(return_value=OPEN_PR | {"auto_merge": {"merge_method": "merge"}})
    api.post = Mock(return_value=graphql_reply())
    assert api.set_auto_merge(MERGE_REPO, 7, enabled=False) == {"auto_merge": False, "merged": False}
    assert "disablePullRequestAutoMerge" in api.post.call_args.args[1]["query"]


def test_auto_merge_already_in_the_asked_state_writes_nothing(api):
    api.get = Mock(return_value=OPEN_PR)
    api.post = Mock()
    assert api.set_auto_merge(MERGE_REPO, 7, enabled=False) == {"auto_merge": False, "merged": False}
    api.post.assert_not_called()


@pytest.mark.parametrize(
    ("repo", "pr", "message"),
    [
        (MERGE_REPO, OPEN_PR | {"state": "closed"}, "no longer open"),
        (MERGE_REPO, OPEN_PR | {"draft": True}, "ready for review"),
        (MERGE_REPO | {"allow_auto_merge": False}, OPEN_PR, "Allow auto-merge"),
    ],
)
def test_auto_merge_is_refused_before_writing(api, repo, pr, message):
    api.get = Mock(return_value=pr)
    api.post = Mock()
    with pytest.raises(github.DashboardError, match=message):
        api.set_auto_merge(repo, 7, enabled=True)
    api.post.assert_not_called()


def test_a_github_refusal_is_reported(api):
    api.get = Mock(return_value=OPEN_PR)
    api.post = Mock(return_value=graphql_reply([{"message": "Resource not accessible by personal access token"}]))
    with pytest.raises(github.DashboardError, match="not accessible"):
        api.set_auto_merge(MERGE_REPO, 7, enabled=True)


@responses.activate
def test_a_clean_pr_with_finished_checks_is_merged_at_its_checked_head(api):
    api.get = Mock(return_value=OPEN_PR)
    api.post = Mock(return_value=graphql_reply([{"message": "Pull request is in clean status"}]))
    api.pull = Mock(return_value={"checks": [{"pending": False, "state": "success"}]})
    merge = responses.put(f"{ROOT}/repos/alice/project/pulls/7/merge", json={"merged": True})
    assert api.set_auto_merge(MERGE_REPO, 7, enabled=True) == {"auto_merge": False, "merged": True}
    assert _json_body(merge.calls[0]) == {"merge_method": "merge", "sha": "abc"}


@pytest.mark.parametrize("check", [{"pending": True, "state": "in_progress"}, {"pending": False, "state": "failure"}])
def test_a_clean_pr_with_unfinished_or_failed_checks_is_not_merged(api, check):
    api.get = Mock(return_value=OPEN_PR)
    api.post = Mock(return_value=graphql_reply([{"message": "Pull request is in clean status"}]))
    api.pull = Mock(return_value={"checks": [check]})
    with pytest.raises(github.DashboardError, match="main requires no checks"):
        api.set_auto_merge(MERGE_REPO, 7, enabled=True)
