"""The developer panel's data: the owner's GitHub at a glance and their AI plans.

The developer provider (broker/providers/developer.py) polls it and writes it to the store, where the renderers read it.
"""

from typing import TYPE_CHECKING

from broker.clients import ai_plans, github

if TYPE_CHECKING:
    from datetime import date

    from redis import Redis


def snapshot(token: str | None, today: date, cache: Redis | None = None) -> dict:
    """Return `{"github": ..., "plans": [...]}`; `github` is None without a GitHub `token`.

    GitHub is read through `cache`'s ETags when given (github.summary).

    Raises requests.RequestException if GitHub can't be reached or refuses the token.
    """
    summary = copilot_used = None
    if token is not None:
        summary = github.summary(token, cache)
        copilot_used = github.copilot_premium_requests(token, summary["login"])
    return {"github": summary, "plans": ai_plans.plans(today, copilot_used, has_copilot=token is not None)}
