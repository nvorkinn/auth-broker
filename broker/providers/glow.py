"""The glow-provider: every Glow device's energy readings, fetched from Glowmarkt and written to Redis.

Run it from the broker's image: `python -m broker.providers.glow`. It reads the broker's database (read-only) for the
devices with a Glowmarkt account, decrypts their passwords with CREDENTIAL_ENCRYPTION_KEY, and writes each device's
readings to `v1:energy:<device id>` with a TTL, so the passwords never leave the broker. Renderers that read the store
(pi-dashboard's GlowClient) draw the energy panel from there; standalone ones still call Glowmarkt with the
credentials in their config.

The value is Glowmarkt's own answers, as they came, in a thin wrapper (see "The energy provider" in the README):

    {"fetched_at": <ISO 8601, UTC>, "device_id": ..., "offset": <Glowmarkt's offset param, e.g. "-60">,
     "resource_id": ..., "hourly": <readings, PT1H>, "daily": <readings, P1D>, "monthly": <readings, P1M>}

The loop is pi-dashboard's provider runner's (countdown_providers.runner), in miniature: a cycle every INTERVAL_S,
on the epoch-aligned tick, or sooner when the broker publishes on `v1:sub:changed`; a device that fails is logged and
skipped (its key expires if it keeps failing); an unreachable Redis is logged and retried on the next tick; SIGTERM
and SIGINT stop it between requests.
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
from cryptography.fernet import InvalidToken
from flask import Flask
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError

from broker.clients.glowmarkt import DAILY, HOURLY, MONTHLY, PERIODS, GlowmarktAccount, local_now, utc_offset_param
from broker.clients.glowmarkt import window_start as page_window_start
from broker.db import _db_path, db
from broker.log_format import JsonFormatter
from broker.models import DeviceConfig, GlowmarktCredentials
from broker.routes.devices import _timezone
from broker.routes.pages import postcode_country

if TYPE_CHECKING:
    from collections.abc import Callable

logger = logging.getLogger("broker.providers.glow")

SOURCE = "glow"
SUB_CHANGED = "v1:sub:changed"
# Glowmarkt has a new half-hour's reading at most every 30 minutes (DCC data often lags by hours), and the panel's
# pages are hours, days and months: every 15 minutes keeps the open hour's bar within a quarter of an hour, at three
# readings requests per device, fewer than a renderer's own (each page every 10 minutes).
INTERVAL_S = 15 * 60
# Four cycles: a blip of a cycle or two goes unnoticed, while after an hour without a fetch (the provider down, or
# Glowmarkt failing) the key is gone and the renderers stop drawing new pages (each keeps its last).
TTL_S = 60 * 60
# How long to wait before asking Redis again for the nudges, when it can't be reached.
RESUBSCRIBE_S = 5
# The renderer's default zone when its config has none (the countdown-api contract's).
DEFAULT_ZONE = "Europe/London"


def energy_key(device_id: str) -> str:
    """Return the key of a device's energy readings (countdown_core.store.keys.energy in pi-dashboard)."""
    return f"v1:energy:{device_id}"


@dataclass(frozen=True)
class Account:
    """A device's Glowmarkt account, decrypted, and the time zone its screen goes by."""

    device_id: str
    username: str
    password: str
    zone: str


def create_app() -> Flask:
    """Return an app that can only read the broker's database and decrypt what's in it.

    Not broker.create_app(): that migrates the database and wants every one of the broker's secrets. This opens the
    database read-only (SQLite's mode=ro), so the provider can never write to it, nor hold a write lock.
    """
    app = Flask(__name__)
    app.config["CREDENTIAL_ENCRYPTION_KEY"] = os.environ["CREDENTIAL_ENCRYPTION_KEY"]
    app.config["SQLALCHEMY_DATABASE_URI"] = f"sqlite:///file:{_db_path()}?mode=ro&uri=true"
    db.init_app(app)
    return app


def accounts() -> list[Account]:
    """Return the devices with a usable Glowmarkt account: the broker's `v1:sub:glow`, less any without a username.

    The same query the broker's subscriptions are made from, in one short read, so the broker's writes never wait on
    it. Needs an app context. A password that won't decrypt (CREDENTIAL_ENCRYPTION_KEY changed) is logged and skipped.
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
            # A renderer only turns the panel on with both (countdown_core.core.api_registry.is_enabled).
            if not creds.username.strip():
                continue
            try:
                password = creds.password or ""
            except InvalidToken:
                logger.warning("Can't decrypt the Glowmarkt password of device %s", creds.device_id)
                continue
            if not password.strip():
                continue
            zone = _timezone(postcode_country(config.postcode), config.tfl_stop_ids) if config else DEFAULT_ZONE
            found.append(Account(creds.device_id, creds.username.strip(), password, zone))
        return found
    finally:
        db.session.remove()


def stored_value(account: Account, glow: GlowmarktAccount, now: datetime) -> dict[str, Any]:
    """Fetch the device's three pages of readings, as of `now`, and return what's stored for it.

    Each page's window is the renderer's (GlowClient._page_config), in the screen's zone at today's offset, from the
    start of its first bucket to now, so the renderer can draw any page from it, as of when it draws.
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
        """Stop after the request in flight (a signal handler)."""
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
    provider = GlowProvider(create_app(), store)
    signal.signal(signal.SIGTERM, provider.stop)
    signal.signal(signal.SIGINT, provider.stop)
    provider.serve()


if __name__ == "__main__":
    main()
