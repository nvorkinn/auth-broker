from unittest.mock import patch

import pytest
import requests

from broker.clients import github


@pytest.fixture
def dashboard(app):
    app.config.update(GITHUB_DASHBOARD_USER_ID="user-1", GITHUB_DASHBOARD_TOKEN="test-pat")
    with patch("broker.routes.github_dashboard.github.Dashboard") as api:
        yield api.return_value


def csrf(client):
    assert client.get("/github").status_code == 200
    with client.session_transaction() as session:
        return {"X-CSRF-Token": session["github_dashboard_csrf"]}


def test_disabled_by_default(client):
    assert client.get("/github").status_code == 503


def test_authentication_and_personal_authorization(client, anonymous_client, dashboard):
    assert anonymous_client.get("/github").status_code == 401
    assert client.get("/github", headers={"X-Authentik-Uid": "someone-else"}).status_code == 403
    assert not dashboard.repositories.called


def test_dashboard_never_exposes_the_pat(client, dashboard):
    response = client.get("/github")
    assert response.status_code == 200
    assert b"test-pat" not in response.data
    assert response.headers["Cache-Control"] == "no-store"
    assert b"csrf-token" in response.data


def test_repositories_and_project(client, dashboard):
    dashboard.repositories.return_value = [{"full_name": "alice/project"}]
    assert client.get("/github/repositories").json == {"repositories": [{"full_name": "alice/project"}]}
    dashboard.owned_repository.return_value = {"full_name": "alice/project"}
    dashboard.project.return_value = {"pulls": [], "workflows": [], "active": False}
    assert client.get("/github/repositories/alice/project").json["active"] is False
    dashboard.owned_repository.assert_called_once_with("alice", "project")
    dashboard.close.assert_called()


@pytest.mark.parametrize("headers", [{}, {"X-CSRF-Token": "wrong"}, {"X-CSRF-Token": "non-ascii-\u00e9"}])
def test_release_requires_csrf(client, dashboard, headers):
    csrf(client)
    assert client.post("/github/repositories/alice/project/release", headers=headers).status_code == 403
    dashboard.publish_release.assert_not_called()


def test_release_rechecks_owner_and_publishes(client, dashboard):
    dashboard.owned_repository.return_value = {"full_name": "alice/project"}
    dashboard.publish_release.return_value = {"tag_name": "v1.20.0", "html_url": "https://github.com/release"}
    response = client.post("/github/repositories/alice/project/release", headers=csrf(client))
    assert response.status_code == 201
    assert response.json["tag"] == "v1.20.0"
    dashboard.owned_repository.assert_called_once_with("alice", "project")
    dashboard.publish_release.assert_called_once_with({"full_name": "alice/project"})


def test_rate_limit_is_explicit_and_preserves_backoff(client, dashboard):
    upstream = requests.Response()
    upstream.status_code = 429
    upstream.headers["Retry-After"] = "120"
    dashboard.repositories.side_effect = requests.HTTPError(response=upstream)
    response = client.get("/github/repositories")
    assert response.status_code == 429
    assert response.headers["Retry-After"] == "120"
    dashboard.close.assert_called_once()


def test_publication_failure_warns_about_ambiguous_success(client, dashboard):
    dashboard.publish_release.side_effect = requests.Timeout()
    response = client.post("/github/repositories/alice/project/release", headers=csrf(client))
    assert response.status_code == 502
    assert "may have succeeded" in response.json["error"]
    dashboard.close.assert_called_once()


def test_invalid_repository_and_release_conflicts(client, dashboard):
    assert client.get("/github/repositories/alice/..").status_code == 400
    dashboard.publish_release.side_effect = github.DashboardError("Tag already exists.")
    response = client.post("/github/repositories/alice/project/release", headers=csrf(client))
    assert response.status_code == 409
    assert response.json["error"] == "Tag already exists."


def test_dashboard_returns_public_message_not_exception_diagnostics(client, dashboard, caplog):
    error = github.DashboardError("The workflow file could not be parsed.")
    error.args = ("private-workflow-content at internal/path.py:123",)
    dashboard.project.side_effect = error
    response = client.get("/github/repositories/alice/project")
    assert response.status_code == 409
    assert response.json == {"error": "The workflow file could not be parsed."}
    assert "private-workflow-content" not in response.get_data(as_text=True)
    assert "private-workflow-content" in caplog.text
