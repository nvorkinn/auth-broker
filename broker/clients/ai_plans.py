"""The AI plans the developer panel shows: what each is, when it renews, and how much is used where that's known.

Copilot's usage comes from GitHub. Claude and Gemini have no API to ask yet, so they're placeholders here: when one
turns up, its plan is built from it in place of its STUB_PLANS entry, and nothing else changes.
"""

from calendar import monthrange
from datetime import date

DECEMBER = 12

# Copilot Pro's monthly premium requests, which reset on the 1st (GitHub reports usage, not the allowance).
COPILOT_PLAN = "Pro"
COPILOT_ALLOWANCE = 300

# Placeholders until there's an API to ask: the name, the plan, and the day of the month it renews.
STUB_PLANS = (("Claude", "Max", 3), ("Gemini", "AI Pro", 22))


def plans(today: date, copilot_used: int | None, *, has_copilot: bool) -> list[dict]:
    """Return the plans, the developer panel's `plans`: Copilot's (if `has_copilot`), then the placeholders."""
    found = []
    if has_copilot:
        found.append(
            {
                "name": "Copilot",
                "plan": COPILOT_PLAN,
                "renews": next_renewal(today, 1).isoformat(),
                "used": copilot_used,
                "limit": COPILOT_ALLOWANCE if copilot_used is not None else None,
            }
        )
    found += [
        {"name": name, "plan": plan, "renews": next_renewal(today, day).isoformat(), "used": None, "limit": None}
        for name, plan, day in STUB_PLANS
    ]
    return found


def next_renewal(today: date, day: int) -> date:
    """Return the next date, today or later, that falls on `day` of the month (the month's last, if it's shorter)."""
    year, month = today.year, today.month
    if today.day > min(day, monthrange(year, month)[1]):
        year, month = (year + 1, 1) if month == DECEMBER else (year, month + 1)
    return date(year, month, min(day, monthrange(year, month)[1]))
