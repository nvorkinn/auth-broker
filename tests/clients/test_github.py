from datetime import UTC, datetime, timedelta
from unittest.mock import Mock, patch

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


def _github(url, params=None, timeout=None):
    params = params or {}
    if url.endswith("/user"):
        return _response({"login": "nvorkinn"})
    if url.endswith("/user/repos"):
        return _response(REPOS if params["page"] == 1 else [])
    if url.endswith("/search/issues"):
        return _response({"total_count": 3 if "author:nvorkinn" in params["q"] else 1})
    if "/actions/runs" in url:
        full_name = url.split("/repos/")[1].split("/actions")[0]
        assert params["branch"] == "main"
        return _response({"workflow_runs": RUNS[full_name]})
    raise AssertionError(url)


def test_summary():
    with patch("requests.Session.get", side_effect=_github) as get:
        summary = github.summary("github_pat_abc")

    assert summary == {"login": "nvorkinn", "repos": 3, "open_prs": 3, "review_requests": 1, "failing": ["auth-broker"]}
    # The stale repo's builds aren't asked about.
    assert not any("old-thing" in call.args[0] for call in get.call_args_list)


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
