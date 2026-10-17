"""Glowmarkt (Glow's smart-meter API): signing in to a device's account and fetching its electricity readings.

The minimum of the renderer's GlowClient (pi-dashboard, countdown_core/glow/glow_client.py) the glow-provider needs:
the same sign-in, the same "electricity consumption" resource and the same readings requests, so what it stores is
what that renderer would have fetched itself. Answers are kept as Glowmarkt sent them: the renderer parses them.
"""

from datetime import datetime, timedelta, timezone, tzinfo
from http import HTTPStatus
from typing import Any

import requests

BASE_URL = "https://api.glowmarkt.com/api/v0-1"
# The Glow app's application id, as the renderer sends it.
APPLICATION_ID = "b0f1b774-a586-4f72-9edd-27ead8aa7a8d"
TIMEOUT = 10

# The renderer's three pages, by Glowmarkt period: the last 24 hours by hour, the last 31 days by day, and this year
# by month. Each is fetched from the start of its window's first bucket to now, as GlowClient._page_config has them.
HOURLY, DAILY, MONTHLY = "PT1H", "P1D", "P1M"
PERIODS = (HOURLY, DAILY, MONTHLY)


class GlowmarktError(Exception):
    """Glowmarkt couldn't be asked, refused the account, or answered with something unexpected."""


def local_now(zone: tzinfo, now: datetime) -> datetime:
    """Return `now` in `zone` with a fixed offset, as the renderer's `datetime.now().astimezone()` gives it.

    A fixed offset, not the zone: every window start (1st January too) then carries today's offset, as the renderer's
    do, so the readings are asked for, and labelled, at the offset Glowmarkt aggregates them by.
    """
    local = now.astimezone(zone)
    offset = local.utcoffset() or timedelta()
    return local.astimezone(timezone(offset))


def utc_offset_param(local: datetime) -> str:
    """Return Glowmarkt's offset parameter for an aware time: minutes to add to local time to get UTC (BST: "-60")."""
    offset = local.utcoffset() or timedelta()
    return str(round(-offset.total_seconds() / 60))


def window_start(period: str, local: datetime) -> datetime:
    """Return the start of the first bucket of `period`'s page as of `local` (GlowClient._page_config, truncated)."""
    if period == HOURLY:
        return (local - timedelta(hours=24)).replace(minute=0, second=0, microsecond=0)
    if period == DAILY:
        return (local - timedelta(days=31)).replace(hour=0, minute=0, second=0, microsecond=0)
    if period == MONTHLY:
        return local.replace(month=1, day=1, hour=0, minute=0, second=0, microsecond=0)
    msg = f"Unknown period {period!r}"
    raise ValueError(msg)


class GlowmarktAccount:
    """One Glowmarkt account, signed in lazily; its token is kept between calls and renewed when it's refused."""

    def __init__(self, username: str, password: str, session: requests.Session | None = None) -> None:
        """Sign in as `username` (only when first needed), over `session` (by default, a new one)."""
        self.username = username
        self.password = password
        self.session = session or requests.Session()
        self.token: str | None = None
        self.resource_id: str | None = None

    def _authenticate(self) -> str:
        response = self.session.post(
            f"{BASE_URL}/auth",
            json={"username": self.username, "password": self.password, "applicationId": APPLICATION_ID},
            timeout=TIMEOUT,
        )
        response.raise_for_status()
        token = response.json().get("token")
        if not token:
            msg = "Glowmarkt answered the sign-in without a token"
            raise GlowmarktError(msg)
        self.token = token
        return token

    def _get(self, endpoint: str, params: dict[str, str] | None = None) -> Any:  # noqa: ANN401 -- Glowmarkt's JSON
        """GET an endpoint as this account, signing in first if need be, and again (once) if the token's refused."""
        token = self.token or self._authenticate()
        response = self._send(endpoint, token, params)
        if response.status_code == HTTPStatus.UNAUTHORIZED:
            response = self._send(endpoint, self._authenticate(), params)
        response.raise_for_status()
        return response.json()

    def _send(self, endpoint: str, token: str, params: dict[str, str] | None) -> requests.Response:
        headers = {"token": token, "applicationId": APPLICATION_ID, "Content-Type": "application/json"}
        return self.session.get(f"{BASE_URL}{endpoint}", params=params, headers=headers, timeout=TIMEOUT)

    def electricity_resource_id(self) -> str:
        """Return the id of the meter's electricity consumption (looked up once), the resource every page reads."""
        if self.resource_id is None:
            entities = self._get("/virtualentity")
            try:
                resources = entities[0]["resources"]
                self.resource_id = next(r["resourceId"] for r in resources if r["name"] == "electricity consumption")
            except (IndexError, KeyError, TypeError, StopIteration) as e:
                msg = "Glowmarkt has no 'electricity consumption' resource for this account"
                raise GlowmarktError(msg) from e
        return self.resource_id

    def readings(self, period: str, start: datetime, end: datetime) -> dict[str, Any]:
        """Return Glowmarkt's answer, as it is, for the kWh used per `period` from `start` to `end` (both local).

        The same request as the renderer's GlowClient._get_readings: local wall-clock times, and the offset of
        `start`, so Glowmarkt buckets by local hours, days and months.
        """
        answer = self._get(
            f"/resource/{self.electricity_resource_id()}/readings",
            params={
                "from": start.strftime("%Y-%m-%dT%H:%M:%S"),
                "to": end.strftime("%Y-%m-%dT%H:%M:%S"),
                "period": period,
                "offset": utc_offset_param(start),
                "function": "sum",
            },
        )
        if not isinstance(answer, dict) or not isinstance(answer.get("data"), list):
            msg = f"Glowmarkt's {period} readings have no data"
            raise GlowmarktError(msg)
        return answer
