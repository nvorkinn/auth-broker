"""The glow-provider: every Glow device's energy readings, fetched from Glowmarkt and written to Redis.

    python -m broker.providers.glow

It runs in provider_loop.read_only_app() and decrypts the Glowmarkt passwords itself, so they never leave the broker.
Each device's readings go to `v1:energy:<device id>`: Glowmarkt's own answers in a thin wrapper (countdown-api's
StoredEnergy).

Its loop is its own: a cycle at once, then on every epoch-aligned tick of INTERVAL_S, or sooner on `v1:sub:changed`,
when only accounts new or changed since the last tick are fetched. A device that fails is logged and skipped (its key
expires if it keeps failing); an unreachable Redis is logged and tried again on the next tick. SIGTERM and SIGINT stop
it between devices.
"""

import contextlib
import json
import logging
import os
import signal
import sys
import threading
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import redis
from countdown_api import keys as store_keys
from cryptography.fernet import InvalidToken
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError

from broker.clients.glowmarkt import DAILY, HOURLY, MONTHLY, PERIODS, GlowmarktAccount, local_now, utc_offset_param
from broker.clients.glowmarkt import window_start as page_window_start
from broker.db import db
from broker.log_format import JsonFormatter
from broker.models import DeviceConfig, GlowmarktCredentials
from broker.provider_loop import read_only_app
from broker.services.postcodes import postcode_country
from broker.services.renderer_config import timezone

if TYPE_CHECKING:
    from collections.abc import Callable

    from flask import Flask

logger = logging.getLogger("broker.providers.glow")

SOURCE = store_keys.GLOW
SUB_CHANGED = store_keys.SUB_CHANGED
# Glowmarkt has a new half-hour's reading at most every 30 minutes (DCC data often lags by hours): every 15 minutes
# keeps the open hour's bar within a quarter of an hour, at three readings requests per device.
INTERVAL_S = 15 * 60
# About a day (the store contract's): Glowmarkt failing for hours changes nothing on the screens, while after a day
# without a fetch the key is gone and the energy panel says it's not available. The renderers keep no copy, so this
# is the only expiry.
TTL_S = store_keys.ENERGY_TTL_S
# How long to wait before asking Redis again for the nudges, when it can't be reached.
RESUBSCRIBE_S = 5
# The renderer's default zone when its config has none (the countdown-api contract's).
DEFAULT_ZONE = "Europe/London"


def energy_key(device_id: str) -> str:
    """Return the key of a device's energy readings (the store contract's)."""
    return store_keys.energy(device_id)


@dataclass(frozen=True)
class Account:
    """A device's Glowmarkt account, decrypted, and the time zone its screen goes by."""

    device_id: str
    username: str
    password: str
    zone: str


def accounts() -> list[Account]:
    """Return the devices with a usable Glowmarkt account: `v1:sub:glow`, less any with a blank username or password.

    One short read, so the broker's writes don't wait on it. Needs an app context. A password that won't decrypt
    (CREDENTIAL_ENCRYPTION_KEY changed) is logged and skipped.
    """
    try:
        rows = db.session.execute(
            select(GlowmarktCredentials, DeviceConfig)
            .outerjoin(DeviceConfig, DeviceConfig.device_id == GlowmarktCredentials.device_id)
            .where(GlowmarktCredentials.password_encrypted.is_not(None))
            .order_by(GlowmarktCredentials.device_id)
        ).all()
        found = []
        for creds, config in rows:
            # The same rule as the config's glowmarkt.enabled, which turns the renderer's panel on.
            try:
                sign_in = creds.sign_in()
            except InvalidToken:
                logger.warning("Can't decrypt the Glowmarkt password of device %s", creds.device_id)
                continue
            if sign_in is None:
                continue
            zone = timezone(postcode_country(config.postcode), config.tfl_stop_ids) if config else DEFAULT_ZONE
            found.append(Account(creds.device_id, *sign_in, zone))
        return found
    finally:
        db.session.remove()


def stored_value(account: Account, glow: GlowmarktAccount, now: datetime) -> dict[str, Any]:
    """Fetch the device's three pages of readings, as of `now`, and return what's stored for it.

    Each page's window is the renderer's (GlowClient._page_config), in the screen's zone at today's offset, from the
    start of its first bucket to now, so the renderer can cut any page from it when it draws.
    """
    try:
        zone = ZoneInfo(account.zone)
    except ZoneInfoNotFoundError, ValueError:
        zone = ZoneInfo(DEFAULT_ZONE)
    local = local_now(zone, now)
    answers = {period: glow.readings(period, page_window_start(period, local), local) for period in PERIODS}
    return {
        "fetched_at": now.astimezone(UTC).isoformat(),
        "device_id": account.device_id,
        "offset": utc_offset_param(local),
        "resource_id": glow.electricity_resource_id(),
        "hourly": answers[HOURLY],
        "daily": answers[DAILY],
        "monthly": answers[MONTHLY],
    }


class GlowProvider:
    """The loop: fetch every account's readings, write them with a TTL, wait for the next tick or a nudge."""

    def __init__(  # noqa: PLR0913 -- the clocks and the Glowmarkt client are there for the tests
        self,
        app: Flask,
        store: redis.Redis,
        *,
        interval_s: int = INTERVAL_S,
        ttl_s: int = TTL_S,
        clock: Callable[[], float] = time.time,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
        new_account: Callable[[str, str], GlowmarktAccount] = GlowmarktAccount,
    ) -> None:
        """Read accounts with `app`, write to `store`; `clock` (epoch seconds) times the ticks, `now` dates the data."""
        self.app = app
        self.store = store
        self.interval_s = interval_s
        self.ttl_s = ttl_s
        self.clock = clock
        self.now = now
        self.new_account = new_account
        self.stopping = threading.Event()
        # Kept between cycles, so a token is reused until Glowmarkt refuses it. By device id.
        self._glow: dict[str, GlowmarktAccount] = {}
        # The accounts written since the last tick, which a nudge needn't fetch again.
        self._written: set[Account] = set()
        self._pubsub: redis.client.PubSub | None = None

    def stop(self, *_: object) -> None:
        """Stop after the device being fetched. The SIGTERM and SIGINT handler."""
        self.stopping.set()

    def run_cycle(self, *, nudged: bool = False) -> int:
        """Fetch and write every account's readings, and return how many were written.

        A nudged cycle (a config was saved) only fetches accounts that are new or changed since the last tick.
        """
        try:
            with self.app.app_context():
                wanted = accounts()
        except SQLAlchemyError:
            logger.warning("Couldn't read the Glowmarkt accounts; trying again next tick", exc_info=True)
            return 0
        # Forget devices that are gone, and accounts whose credentials changed.
        self._glow = {a.device_id: self._glow[a.device_id] for a in wanted if self._is_current(a)}
        self._written &= set(wanted)

        values = {}
        written = []
        for account in wanted:
            if self.stopping.is_set():
                break
            if nudged and account in self._written:
                continue
            glow = self._glow.get(account.device_id)
            if glow is None:
                glow = self._glow[account.device_id] = self.new_account(account.username, account.password)
            try:
                values[energy_key(account.device_id)] = json.dumps(stored_value(account, glow, self.now()))
            except Exception:  # one device's failure, whatever it is, mustn't stop the others
                logger.warning("Couldn't fetch the energy readings of device %s", account.device_id, exc_info=True)
                continue
            written.append(account)
        if not values:
            return 0
        try:
            with self.store.pipeline(transaction=False) as pipe:
                for key, value in values.items():
                    pipe.set(key, value, ex=self.ttl_s)
                pipe.execute()
        except redis.RedisError:
            logger.warning("Couldn't write %d devices' energy to Redis; trying again next tick", len(values))
            return 0
        self._written.update(written)
        logger.info("Wrote the energy of %d of %d devices", len(values), len(wanted))
        return len(values)

    def _is_current(self, account: Account) -> bool:
        glow = self._glow.get(account.device_id)
        return glow is not None and (glow.username, glow.password) == (account.username, account.password)

    def serve(self) -> None:
        """Run cycles until stopped: on every tick of the interval, and whenever the subscriptions change."""
        logger.info("Fetching Glowmarkt readings every %ds, kept for %ds", self.interval_s, self.ttl_s)
        nudged = False
        while not self.stopping.is_set():
            if not nudged:
                self._written.clear()
            self.run_cycle(nudged=nudged)
            next_tick = (self.clock() // self.interval_s + 1) * self.interval_s
            nudged = self._wait(next_tick)
        self._unsubscribe()
        logger.info("Stopped")

    def _wait(self, deadline: float) -> bool:
        """Wait until `deadline` (epoch seconds), a nudge on `v1:sub:changed` or a stop; return whether nudged."""
        while not self.stopping.is_set():
            remaining = deadline - self.clock()
            if remaining <= 0:
                return False
            try:
                if self._pubsub is None:
                    self._pubsub = self.store.pubsub(ignore_subscribe_messages=True)
                    self._pubsub.subscribe(SUB_CHANGED)
                # At most a second at a time, so a stop is seen promptly.
                if self._pubsub.get_message(timeout=min(remaining, 1.0)) is not None:
                    logger.debug("Subscriptions changed")
                    return True
            except redis.RedisError:
                logger.debug("Can't listen for subscription changes", exc_info=True)
                self._unsubscribe()
                self.stopping.wait(min(remaining, RESUBSCRIBE_S))
        return False

    def _unsubscribe(self) -> None:
        if self._pubsub is not None:
            with contextlib.suppress(redis.RedisError):
                self._pubsub.close()
            self._pubsub = None


def main() -> None:
    """Run the glow-provider until SIGTERM or SIGINT."""
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO").upper(), handlers=[handler])
    store = redis.Redis.from_url(
        os.environ.get("REDIS_URL", "redis://127.0.0.1:6379/0"),
        password=os.environ.get("REDIS_PASSWORD") or None,
        decode_responses=True,
        socket_timeout=5,
        socket_connect_timeout=5,
    )
    provider = GlowProvider(read_only_app(), store)
    signal.signal(signal.SIGTERM, provider.stop)
    signal.signal(signal.SIGINT, provider.stop)
    provider.serve()


if __name__ == "__main__":
    main()
