"""The developer panel's data (broker/services/developer.py), which the developer provider writes to the store."""

from datetime import date
from unittest.mock import patch

import pytest
import requests

from broker.services import developer

SUMMARY = {"login": "nvorkinn", "repos": 9, "open_prs": 3, "review_requests": 1, "failing": ["auth-broker"]}
TODAY = date(2026, 10, 18)


def test_without_a_token_only_the_placeholder_plans_and_github_is_not_asked():
    with patch("broker.clients.github.summary") as summary:
        answer = developer.snapshot(None, TODAY)

    summary.assert_not_called()
    assert answer["github"] is None
    assert [plan["name"] for plan in answer["plans"]] == ["Claude", "Gemini"]


def test_with_a_token_github_and_copilot_too():
    with (
        patch("broker.clients.github.summary", return_value=SUMMARY) as summary,
        patch("broker.clients.github.copilot_premium_requests", return_value=212) as copilot,
    ):
        answer = developer.snapshot("github_pat_abc", TODAY)

    summary.assert_called_once_with("github_pat_abc")
    copilot.assert_called_once_with("github_pat_abc", "nvorkinn")
    assert answer["github"] == SUMMARY
    assert [plan["name"] for plan in answer["plans"]] == ["Copilot", "Claude", "Gemini"]
    assert (answer["plans"][0]["used"], answer["plans"][0]["limit"]) == (212, 300)
    # The plans renew from the day given, not the clock's.
    assert answer["plans"][0]["renews"] == "2026-11-01"


@pytest.mark.parametrize("failing", ["summary", "copilot_premium_requests"])
def test_github_failing_raises_so_the_provider_writes_nothing(failing):
    with (
        patch("broker.clients.github.summary", return_value=SUMMARY),
        patch("broker.clients.github.copilot_premium_requests", return_value=212),
        patch(f"broker.clients.github.{failing}", side_effect=requests.HTTPError("401 Unauthorized")),
        pytest.raises(requests.RequestException),
    ):
        developer.snapshot("github_pat_abc", TODAY)
