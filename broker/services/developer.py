"""The developer panel's data: the owner's GitHub at a glance and their AI plans.

One answer for both of its readers: the `/api/developer` route (for renderers that don't read the store) and the
developer provider (which writes it there).
"""

from typing import TYPE_CHECKING

from broker.clients import ai_plans, github

if TYPE_CHECKING:
    from datetime import date


def snapshot(token: str | None, today: date) -> dict:
    """Return `{"github": ..., "plans": [...]}`; `github` is None without a GitHub `token`.

    Raises requests.RequestException if GitHub can't be reached or refuses the token.
    """
    summary = copilot_used = None
    if token is not None:
        summary = github.summary(token)
        copilot_used = github.copilot_premium_requests(token, summary["login"])
    return {"github": summary, "plans": ai_plans.plans(today, copilot_used, has_copilot=token is not None)}
