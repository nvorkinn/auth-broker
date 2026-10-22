"""The developer provider: each screen's developer panel, written to the shared Redis store.

    python -m broker.providers.developer

It runs on providers/loop.py's loop, in read_only_app(): GitHub's tokens aren't refreshed, so there is nothing to write
back. For every device with the developer panel on it polls the owner's GitHub and AI plans and writes them to
`v1:developer:<device id>`.

While the panel shows a release (running, or finished within github.SHOWN_AFTER), its jobs change by the minute, so
the provider polls every BURST_INTERVAL_S, asks for the screen to refresh as fast with
`v1:pace:developer:<device id>`, and queues a render of the screen after each write (services/render_jobs.py), plus one
more on the cycle that finds the release gone. Otherwise the pace key is deleted and the minute's tick draws the
screen.

A device whose token GitHub rate-limits isn't polled again until GitHub says (github.rate_limit_wait), and its keys are
left as they are meanwhile.
"""

import json
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import requests
from countdown_api import keys as store_keys
from sqlalchemy import select

from broker.clients import github
from broker.db import db
from broker.models import DeviceConfig, GitHubToken
from broker.providers import loop
from broker.providers.loop import ProviderLoop
from broker.services import developer, render_jobs

if TYPE_CHECKING:
    from collections.abc import Callable

    import redis
    from flask import Flask

# Pull requests and builds change slowly, but a release that starts is worth catching soon. A full poll costs about
# 25 GitHub calls, so this uses about a sixth of a token's 5,000 an hour.
IDLE_INTERVAL_S = 120
# While a release shows, its jobs change by the minute (and a finished one must go once its minute is up).
BURST_INTERVAL_S = 20
# The pace asked of the screen while a release shows: the fastest the broker allows (services/pace.py's HINT_FLOOR_S).
PACE_S = 20
# The pace key lapses unless the next cycle renews it: three burst cycles, so one missed poll doesn't slow the screen.
PACE_TTL_S = store_keys.PACE_TTL_S
# Short enough that a provider down for a while shows as missing data, not stale. The store contract's, as are the
# keys.
TTL_S = store_keys.DEVELOPER_TTL_S


def stored_key(device_id: str) -> str:
    """Return the key of a device's developer panel (the store contract's)."""
    return store_keys.developer(device_id)


def pace_key(device_id: str) -> str:
    """Return the key of the pace the developer provider asks for (the store contract's)."""
    return store_keys.pace(store_keys.DEVELOPER, device_id)


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
    return [Wanted(device_id, token.token if token else None) for device_id, token in rows]


def release_shown(snapshot: dict) -> bool:
    """Whether the panel shows a release: one running, or one finished within github.SHOWN_AFTER.

    A finished one counts too, so the fast pace lasts until it's gone from the screen.
    """
    return bool((snapshot["github"] or {}).get("releases"))


def stored(fetched_at: datetime, snapshot: dict) -> str:
    """Return the stored JSON: when it was fetched, and the panel's data."""
    return json.dumps({"fetched_at": fetched_at.isoformat(), **snapshot})


class DeveloperProvider(ProviderLoop):
    """Polls every wanted device's GitHub and plans each cycle, faster while one of them shows a release."""

    label = "Developer"

    def __init__(self, app: Flask, store: redis.Redis, clock: Callable[[], float] = time.time) -> None:
        """See ProviderLoop."""
        super().__init__(app, store, clock)
        # The devices whose last poll saw a release to show. A device whose poll fails stays as it was, so one
        # hiccup from GitHub doesn't slow the screen in the middle of a release.
        self._running: set[str] = set()
        # Device -> when its token may be used again, after GitHub rate-limited it. Per device, as GitHub's limits are
        # per token, not per app as Spotify's are.
        self._backoff_until: dict[str, float] = {}

    def interval_s(self) -> float:
        """Return BURST_INTERVAL_S while any device shows a release, else IDLE_INTERVAL_S."""
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
        now = self.clock()
        self._backoff_until = {d: until for d, until in self._backoff_until.items() if until > now}
        written = 0
        for want in wanted:
            if self._stopping.is_set():
                break
            written += self._poll_device(want)
        self.logger.debug("Developer: wrote %d keys for %d devices", written, len(wanted))
        return written

    def _poll_device(self, want: Wanted) -> int:
        now = self.clock()
        if now < self._backoff_until.get(want.device_id, 0):
            self.logger.debug("Developer: skipping device %s, rate-limited by GitHub", want.device_id)
            return 0
        fetched_at = datetime.fromtimestamp(now, UTC)
        try:
            snapshot = developer.snapshot(want.token, fetched_at.date())
        except requests.RequestException as e:
            # Its keys aren't refreshed, so they expire (and the panel says it can't connect) if this keeps up.
            if (wait := github.rate_limit_wait(e, now)) is not None:
                self._backoff_until[want.device_id] = now + wait
                self.logger.warning(
                    "Developer: GitHub is rate-limiting device %s's token; polling it again in %ds",
                    want.device_id,
                    wait,
                )
            else:
                self.logger.warning("Developer: GitHub for device %s: %s", want.device_id, e)
            return 0
        running = release_shown(snapshot)
        # The cycle that finds the release gone ends the burst: one last render takes it off the screen.
        bursting = running or want.device_id in self._running
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
        if bursting:
            # After the write, so the render reads the new data. Never raises: the tick draws the screen anyway.
            render_jobs.enqueue(self.store, [want.device_id])
        return 1 + running


def main() -> None:
    """Run the provider until SIGINT or SIGTERM."""
    loop.serve(DeveloperProvider)


if __name__ == "__main__":
    main()
