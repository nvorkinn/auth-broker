from unittest.mock import patch

import requests

from broker.db import db
from broker.models import DeviceConfig, GitHubToken

SUMMARY = {"login": "nvorkinn", "repos": 9, "open_prs": 3, "review_requests": 1, "failing": ["auth-broker"]}


def _get(client, secret):
    return client.get("/api/developer", headers={"Authorization": f"Bearer {secret}"})


def _switch_on(app, device_id, token=None):
    with app.app_context():
        db.session.get(DeviceConfig, device_id).developer_enabled = True
        if token is not None:
            stored = GitHubToken(device_id=device_id)
            stored.token = token
            db.session.add(stored)
        db.session.commit()


def test_requires_auth(client, register_device):
    register_device()

    assert client.get("/api/developer").status_code == 401


def test_switched_off_it_has_nothing_and_asks_github_nothing(client, register_device):
    _device_id, secret = register_device()

    with patch("broker.clients.github.summary") as summary:
        response = _get(client, secret)

    assert response.get_json() == {"github": None, "plans": []}
    summary.assert_not_called()


def test_without_a_token_only_the_placeholder_plans(app, client, register_device):
    device_id, secret = register_device()
    _switch_on(app, device_id)

    body = _get(client, secret).get_json()

    assert body["github"] is None
    assert [plan["name"] for plan in body["plans"]] == ["Claude", "Gemini"]


def test_with_a_token_github_and_copilot_too(app, client, register_device):
    device_id, secret = register_device()
    _switch_on(app, device_id, token="github_pat_abc")

    with (
        patch("broker.clients.github.summary", return_value=SUMMARY) as summary,
        patch("broker.clients.github.copilot_premium_requests", return_value=212) as copilot,
    ):
        body = _get(client, secret).get_json()

    summary.assert_called_once_with("github_pat_abc")
    copilot.assert_called_once_with("github_pat_abc", "nvorkinn")
    assert body["github"] == SUMMARY
    assert [plan["name"] for plan in body["plans"]] == ["Copilot", "Claude", "Gemini"]
    assert (body["plans"][0]["used"], body["plans"][0]["limit"]) == (212, 300)


def test_github_failing_is_a_502_so_the_screen_keeps_its_last_panel(app, client, register_device):
    device_id, secret = register_device()
    _switch_on(app, device_id, token="github_pat_abc")

    with patch("broker.clients.github.summary", side_effect=requests.HTTPError("401 Unauthorized")):
        response = _get(client, secret)

    assert response.status_code == 502
