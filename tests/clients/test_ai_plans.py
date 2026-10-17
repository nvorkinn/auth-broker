from datetime import date

import pytest

from broker.clients import ai_plans


@pytest.mark.parametrize(
    ("today", "day", "renews"),
    [
        (date(2026, 10, 17), 22, date(2026, 10, 22)),
        (date(2026, 10, 22), 22, date(2026, 10, 22)),  # today counts
        (date(2026, 10, 23), 22, date(2026, 11, 22)),
        (date(2026, 12, 17), 3, date(2027, 1, 3)),
        (date(2026, 2, 10), 31, date(2026, 2, 28)),  # a short month renews on its last day
        (date(2026, 3, 1), 31, date(2026, 3, 31)),
    ],
)
def test_next_renewal(today, day, renews):
    assert ai_plans.next_renewal(today, day) == renews


def test_copilot_resets_on_the_first_and_comes_first():
    plans = ai_plans.plans(date(2026, 10, 17), 212, has_copilot=True)

    assert plans[0] == {"name": "Copilot", "plan": "Pro", "renews": "2026-11-01", "used": 212, "limit": 300}
    assert [plan["name"] for plan in plans] == ["Copilot", "Claude", "Gemini"]


def test_copilot_without_its_usage_has_no_limit_either():
    (copilot, *_) = ai_plans.plans(date(2026, 10, 17), None, has_copilot=True)

    assert (copilot["used"], copilot["limit"]) == (None, None)


def test_the_placeholders_renew_on_their_day():
    plans = ai_plans.plans(date(2026, 10, 17), None, has_copilot=False)

    assert plans == [
        {"name": "Claude", "plan": "Max", "renews": "2026-11-03", "used": None, "limit": None},
        {"name": "Gemini", "plan": "AI Pro", "renews": "2026-10-22", "used": None, "limit": None},
    ]
