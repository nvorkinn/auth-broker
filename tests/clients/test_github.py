import json
from datetime import UTC, datetime, timedelta
from unittest.mock import Mock, patch

import fakeredis
import pytest
import requests

from broker.clients import github

RECENT = (datetime.now(UTC) - timedelta(days=1)).isoformat()
STALE = (datetime.now(UTC) - timedelta(days=90)).isoformat()


def _response(json_data, status_code=200):
    response = Mock()
    response.ok = status_code < 400
    response.status_code = status_code
    response.json.return_value = json_data
    response.text = json.dumps(json_data)
    response.headers = {}
    response.raise_for_status = Mock()
    if not response.ok:
        response.raise_for_status.side_effect = requests.HTTPError(f"{status_code}")
    return response


def _repo(name, pushed_at=RECENT, *, archived=False):
    return {
        "name": name,
        "full_name": f"nvorkinn/{name}",
        "default_branch": "main",
        "pushed_at": pushed_at,
        "archived": archived,
    }


REPOS = [_repo("pi-dashboard"), _repo("auth-broker"), _repo("old-thing", STALE), _repo("attic", archived=True)]
RUNS = {
    # One workflow failing, the other's latest passing, though it failed before.
    "nvorkinn/auth-broker": [
        {"workflow_id": 1, "conclusion": "success"},
        {"workflow_id": 2, "conclusion": "failure"},
        {"workflow_id": 1, "conclusion": "failure"},
    ],
    "nvorkinn/pi-dashboard": [{"workflow_id": 1, "conclusion": "success"}, {"workflow_id": 1, "conclusion": "failure"}],
}


def _github(url, params=None, timeout=None, headers=None):
    params = params or {}
    if url.endswith("/user"):
        return _response({"login": "nvorkinn"})
    if url.endswith("/user/repos"):
        return _response(REPOS if params["page"] == 1 else [])
    if url.endswith("/search/issues"):
        return _response({"total_count": 3 if "author:nvorkinn" in params["q"] else 1})
    if url.endswith("/actions/runs") and "created" in params:
        return _response({"workflow_runs": []})
    if "/actions/runs" in url:
        full_name = url.split("/repos/")[1].split("/actions")[0]
        assert params["branch"] == "main"
        return _response({"workflow_runs": RUNS[full_name]})
    raise AssertionError(url)


def test_summary():
    with patch("requests.Session.get", side_effect=_github) as get:
        summary = github.summary("github_pat_abc")

    assert summary == {
        "login": "nvorkinn",
        "repos": 3,
        "open_prs": 3,
        "review_requests": 1,
        "failing": ["auth-broker"],
        "releases": [],
    }
    # The stale repo's builds aren't asked about.
    assert not any("old-thing" in call.args[0] for call in get.call_args_list)


def test_summary_revalidates_what_it_read_last_time():
    cache = fakeredis.FakeRedis()

    def first(url, params=None, timeout=None, headers=None):
        response = _github(url, params, timeout, headers)
        response.headers = {"ETag": f'"{url}"'}
        return response

    def unchanged(url, params=None, timeout=None, headers=None):
        assert headers["If-None-Match"] == f'"{url}"'
        return _response(None, 304)

    with patch("requests.Session.get", side_effect=first):
        before = github.summary("github_pat_abc", cache)
    with patch("requests.Session.get", side_effect=unchanged):
        assert github.summary("github_pat_abc", cache) == before


def test_summary_sends_the_token():
    session = github._session("github_pat_abc")

    assert session.headers["Authorization"] == "Bearer github_pat_abc"


def test_summary_raises_when_github_refuses_the_token():
    with (
        patch("requests.Session.get", return_value=_response({}, 401)),
        pytest.raises(requests.HTTPError),
    ):
        github.summary("expired")


def test_copilot_premium_requests_adds_up_this_months_usage():
    usage = {"usageItems": [{"grossQuantity": 200, "model": "GPT-5"}, {"grossQuantity": 12.0, "model": "Claude"}]}

    with patch("requests.Session.get", return_value=_response(usage)) as get:
        assert github.copilot_premium_requests("github_pat_abc", "nvorkinn") == 212

    assert get.call_args.args[0].endswith("/users/nvorkinn/settings/billing/premium_request/usage")


def test_copilot_premium_requests_is_none_when_github_wont_say():
    with patch("requests.Session.get", return_value=_response({}, 403)):
        assert github.copilot_premium_requests("github_pat_abc", "nvorkinn") is None


WORKFLOW = """
jobs:
  tag:
    runs-on: ubuntu-latest
  images:
    name: image ${{ matrix.image }}
    needs: tag
    strategy:
      matrix:
        include: [{image: broker}, {image: caddy}]
  deploy:
    needs: [tag, images]
    environment: production
"""
NOW = datetime.now(UTC)


def _release_run(run_id=7, *, status="in_progress", conclusion=None, name="Release", updated=NOW, **fields):
    return {
        "id": run_id,
        "name": name,
        "status": status,
        "conclusion": conclusion,
        "event": "push",
        "head_branch": "v1.27.0",
        "head_sha": "abc123",
        "path": ".github/workflows/release.yml",
        "run_number": 47,
        "run_started_at": (NOW - timedelta(minutes=4)).isoformat(),
        "updated_at": updated.isoformat(),
    } | fields


def _releasing(runs, *, workflow=WORKFLOW, jobs=None):
    """Fake a GitHub with `runs` in auth-broker's last few hours, and the given workflow file and jobs for run 7."""
    jobs = (
        jobs
        if jobs is not None
        else [
            {"name": "tag", "status": "completed", "conclusion": "success"},
            {"name": "image broker", "status": "in_progress", "conclusion": None},
        ]
    )

    def get(url, params=None, timeout=None, headers=None):
        params = params or {}
        if url.endswith("/actions/runs") and "created" in params:
            return _response({"workflow_runs": runs if "auth-broker" in url else []})
        if url.endswith("/actions/runs/7/jobs"):
            return _response({"jobs": jobs})
        if "/contents/" in url:
            assert url.endswith("/repos/nvorkinn/auth-broker/contents/.github/workflows/release.yml")
            assert params == {"ref": "abc123"}
            assert headers == {"Accept": "application/vnd.github.raw+json"}
            if workflow is None:
                return _response({}, 404)
            response = _response({})
            response.text = workflow
            return response
        return _github(url, params, timeout)

    return get


def test_summary_has_a_running_release_with_its_jobs_as_a_graph():
    runs = [_release_run(8, name="CI"), _release_run()]

    with patch("requests.Session.get", side_effect=_releasing(runs)) as get:
        (release,) = github.summary("github_pat_abc")["releases"]

    assert release == {
        "repo": "auth-broker",
        "title": "v1.27.0",
        "state": "running",
        "started": runs[1]["run_started_at"],
        "finished": None,
        "jobs": [
            {"key": "tag", "name": "tag", "state": "done", "needs": [], "done": None, "total": None},
            {"key": "images", "name": "images", "state": "running", "needs": ["tag"], "done": 0, "total": 2},
            {
                "key": "deploy",
                "name": "deploy",
                "state": "queued",
                "needs": ["tag", "images"],
                "done": None,
                "total": None,
            },
        ],
    }
    created = next(
        call.kwargs["params"]["created"] for call in get.call_args_list if "created" in call.kwargs["params"]
    )
    assert created.startswith(">=")


def test_a_release_started_by_hand_is_called_by_its_run_number():
    runs = [_release_run(event="workflow_dispatch", head_branch="main")]

    with patch("requests.Session.get", side_effect=_releasing(runs)):
        (release,) = github.summary("github_pat_abc")["releases"]

    assert release["title"] == "#47"


def test_a_finished_release_shows_for_a_minute_then_not():
    # From the time now, not NOW: that's taken when the module is imported, which under xdist can be long enough
    # before this runs to push a run finished "50 seconds ago" past the minute.
    now = datetime.now(UTC)
    just = _release_run(status="completed", conclusion="success", updated=now - github.SHOWN_AFTER / 2)
    earlier = _release_run(status="completed", conclusion="failure", updated=now - 2 * github.SHOWN_AFTER)

    with patch("requests.Session.get", side_effect=_releasing([just])):
        (release,) = github.summary("github_pat_abc")["releases"]
    with patch("requests.Session.get", side_effect=_releasing([earlier])):
        assert github.summary("github_pat_abc")["releases"] == []

    assert (release["state"], release["finished"]) == ("done", just["updated_at"])


def test_without_the_workflow_file_the_release_still_shows_its_jobs():
    with patch("requests.Session.get", side_effect=_releasing([_release_run()], workflow=None)):
        (release,) = github.summary("github_pat_abc")["releases"]

    assert [(job["key"], job["needs"]) for job in release["jobs"]] == [("tag", []), ("image broker", [])]


def test_a_release_that_cant_be_read_is_left_out_and_the_rest_still_shows(caplog):
    def jobs_refused(url, params=None, timeout=None, headers=None):
        if url.endswith("/jobs"):
            return _response({}, 403)
        return _releasing([_release_run()])(url, params, timeout, headers)

    with patch("requests.Session.get", side_effect=jobs_refused):
        summary = github.summary("github_pat_abc")

    assert summary["releases"] == []
    assert summary["failing"] == ["auth-broker"]
    assert "Couldn't read nvorkinn/auth-broker's release" in caplog.text


def _http_error(status, headers=None, text=""):
    response = requests.Response()
    response.status_code = status
    response.headers.update(headers or {})
    response._content = text.encode()
    return requests.HTTPError(f"{status}", response=response)


@pytest.mark.parametrize(
    ("error", "wait"),
    [
        (_http_error(429, {"Retry-After": "30"}), 30),
        (_http_error(403, {"Retry-After": "0"}), 1),
        (_http_error(403, {"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": "1000600"}), 600),
        (_http_error(429), github.DEFAULT_BACKOFF_S),
        (_http_error(403, text='{"message": "You have exceeded a secondary rate limit."}'), github.DEFAULT_BACKOFF_S),
        (_http_error(403, text='{"message": "Resource not accessible by personal access token"}'), None),
        (_http_error(403, {"X-RateLimit-Remaining": "4999", "X-RateLimit-Reset": "1000600"}), None),
        (_http_error(401), None),
        (_http_error(502, {"Retry-After": "30"}), None),
        (requests.ConnectionError("down"), None),
        (requests.HTTPError("no response"), None),
    ],
)
def test_rate_limit_wait(error, wait):
    assert github.rate_limit_wait(error, now=1_000_000) == wait


def _limited(*_args, **_kwargs):
    response = Mock()
    response.ok = False
    response.raise_for_status = Mock(side_effect=_http_error(429, {"Retry-After": "30"}))
    return response


def test_copilot_premium_requests_raises_a_rate_limit():
    with patch("requests.Session.get", side_effect=_limited), pytest.raises(requests.HTTPError):
        github.copilot_premium_requests("github_pat_abc", "nvorkinn")


def test_a_rate_limit_reading_a_release_is_raised_not_left_out():
    """Every call after it would fail too: the panel would lose its releases (and the screen its pace) for nothing."""

    def answer(url, params=None, timeout=None, headers=None):
        if url.endswith("/actions/runs") and "created" in (params or {}):
            return _limited()
        return _github(url, params, timeout, headers)

    with patch("requests.Session.get", side_effect=answer), pytest.raises(requests.HTTPError):
        github.summary("github_pat_abc")


def test_a_rate_limit_reading_the_workflow_file_is_raised():
    run = {"path": ".github/workflows/release.yml", "head_sha": "abc"}

    with patch("requests.Session.get", side_effect=_limited), pytest.raises(requests.HTTPError):
        github._workflow_jobs(github.Dashboard("github_pat_abc"), REPOS[0], run)
