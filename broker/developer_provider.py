"""The developer provider: each screen's developer panel, written to the shared Redis store.

    python -m broker.developer_provider

It runs from the broker's image with the broker's settings and database. For every device in `v1:sub:developer`'s
terms (the developer panel switched on) it polls everything the panel shows (the owner's GitHub, with the token only
the broker holds, and their AI plans) on its own, whether or not a renderer is looking, and writes it to
`v1:developer:<device id>`. The renderers on the server read that instead of calling /api/developer.

It also sets the pace. While any of a device's releases is running, the panel's stages change by the minute, so the
provider polls every BURST_INTERVAL_S and asks for the screen to refresh as fast, with `v1:pace:developer:<device id>`
(a TTL of PACE_TTL_S, renewed each cycle it still holds). Nothing says a release has ended: the key lapses, and the
screen goes back to its usual pace. See "The pace" in pi-dashboard's countdown README.
"""

import json
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import requests
from sqlalchemy import select

from broker import provider_loop
from broker.db import db
from broker.models import DeviceConfig, GitHubToken
from broker.provider_loop import ProviderLoop
from broker.services import developer

if TYPE_CHECKING:
    from collections.abc import Callable

    import redis
    from flask import Flask

# Pull requests and builds change slowly, but a release that starts is worth catching soon: every full poll costs
# GitHub ~25 calls (of the 5,000 an hour a token gets), which this keeps to a sixth.
IDLE_INTERVAL_S = 120
# While a release runs, its jobs change by the minute.
BURST_INTERVAL_S = 20
# The refresh pace asked of the screen while a release runs: what the renderer will not go under either.
PACE_S = 20
# The pace key lapses unless the next cycle renews it: three burst cycles, so one missed poll doesn't slow the screen.
PACE_TTL_S = 60
# Kept long enough that a provider that is down for a while shows as missing data (a panel that says so), not stale.
TTL_S = 15 * 60


def stored_key(device_id: str) -> str:
    """Return the key of a device's developer panel (pi-dashboard's store.keys.developer)."""
    return f"v1:developer:{device_id}"


def pace_key(device_id: str) -> str:
    """Return the key of the pace the developer provider asks for (pi-dashboard's store.keys.pace)."""
    return f"v1:pace:developer:{device_id}"


@dataclass(frozen=True)
class Wanted:
    """A device whose developer panel is on, and its GitHub token (None until the owner gives one)."""

    device_id: str
    token: str | None


def wanted_devices() -> list[Wanted]:
    """Return the devices with the developer panel on, with the token each has given (decrypted).

    One without a token is still fetched: the plans don't need it.
    """
    rows = db.session.execute(
        select(DeviceConfig.device_id, GitHubToken)
        .outerjoin(GitHubToken, GitHubToken.device_id == DeviceConfig.device_id)
        .where(DeviceConfig.developer_enabled)
        .order_by(DeviceConfig.device_id)
    ).all()
    return [Wanted(device_id, token and token.token) for device_id, token in rows]


def release_running(snapshot: dict) -> bool:
    """Whether any of the owner's releases is running (not one just finished, which stays on the panel a while)."""
    return any(release["state"] == "running" for release in (snapshot["github"] or {}).get("releases", []))


def stored(fetched_at: datetime, snapshot: dict) -> str:
    """Return the stored JSON: when it was fetched, and the panel's data."""
    return json.dumps({"fetched_at": fetched_at.isoformat(), **snapshot})


class DeveloperProvider(ProviderLoop):
    """Polls every wanted device's GitHub and plans each cycle, faster while one of them has a release running."""

    label = "Developer"

    def __init__(self, app: Flask, store: redis.Redis, clock: Callable[[], float] = time.time) -> None:
        """See ProviderLoop."""
        super().__init__(app, store, clock)
        # The devices whose last poll saw a release running. A device whose poll fails stays as it was, so one
        # hiccup from GitHub doesn't slow the screen in the middle of a release.
        self._running: set[str] = set()

    def interval_s(self) -> float:
        """Return BURST_INTERVAL_S while any device has a release running, else IDLE_INTERVAL_S."""
        return BURST_INTERVAL_S if self._running else IDLE_INTERVAL_S

    def run_cycle(self) -> int:
        """Poll every wanted device and write what came back (and its pace); return how many keys were written."""
        with self.app.app_context():
            try:
                wanted = wanted_devices()
            finally:
                # Short transactions: the web app writes to the same SQLite file, and the polls take a while.
                db.session.remove()
        self._running &= {w.device_id for w in wanted}
        written = 0
        for want in wanted:
            if self._stopping.is_set():
                break
            written += self._poll_device(want)
        self.logger.debug("Developer: wrote %d keys for %d devices", written, len(wanted))
        return written

    def _poll_device(self, want: Wanted) -> int:
        fetched_at = datetime.fromtimestamp(self.clock(), UTC)
        try:
            snapshot = developer.snapshot(want.token, fetched_at.date())
        except requests.RequestException as e:
            # Its keys aren't refreshed, so they expire (and the panel says it can't connect) if this keeps up.
            self.logger.warning("Developer: GitHub for device %s: %s", want.device_id, e)
            return 0
        running = release_running(snapshot)
        if running:
            self._running.add(want.device_id)
        else:
            self._running.discard(want.device_id)
        with self.store.pipeline(transaction=False) as pipe:
            pipe.set(stored_key(want.device_id), stored(fetched_at, snapshot), ex=TTL_S)
            if running:
                pipe.set(pace_key(want.device_id), PACE_S, ex=PACE_TTL_S)
            else:
                pipe.delete(pace_key(want.device_id))
            pipe.execute()
        return 1 + running


def main() -> None:
    """Run the provider until SIGINT or SIGTERM."""
    provider_loop.serve(DeveloperProvider)


if __name__ == "__main__":
    main()
