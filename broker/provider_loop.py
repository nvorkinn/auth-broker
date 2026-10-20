"""The loop a provider runs in, and the read-only app, Redis client and logging it runs with.

A cycle runs at once on start, then on every epoch-aligned tick of the provider's interval (which may change between
cycles), or early on a `v1:sub:changed` message (a config was saved). SIGINT and SIGTERM stop it between devices. A
failed cycle is logged and the next tick tries again; while Redis is unreachable the subscription is retried every
RESUBSCRIBE_S, so a missed message only delays a change until the next tick.

The developer provider runs in it; the music and glow providers have loops of their own.
"""

import logging
import os
import signal
import sys
import threading
import time
from abc import ABC, abstractmethod
from contextlib import suppress
from typing import TYPE_CHECKING

import redis
from countdown_api import keys as store_keys
from flask import Flask
from redis.exceptions import RedisError

from broker.db import init_read_only_app

if TYPE_CHECKING:
    from collections.abc import Callable

SUB_CHANGED = store_keys.SUB_CHANGED
# How long each wait for a `v1:sub:changed` message lasts, so a stop is noticed within it.
LISTEN_S = 1.0
# How long to wait before subscribing again after losing Redis.
RESUBSCRIBE_S = 5.0


def next_tick(now: float, interval_s: float) -> float:
    """Return the first multiple of `interval_s` after `now` (epoch seconds): when the next cycle is due."""
    return (now // interval_s + 1) * interval_s


class ProviderLoop(ABC):
    """Runs cycles against `store` (made with decode_responses=True), in `app`'s context, until stop().

    A provider says what a cycle does (run_cycle) and how far apart the ticks are (interval_s).
    """

    # What the provider is called in its logs: "Developer".
    label: str

    def __init__(self, app: Flask, store: redis.Redis, clock: Callable[[], float] = time.time) -> None:
        """`clock` gives the time in epoch seconds (a parameter for the tests)."""
        self.app = app
        self.store = store
        self.clock = clock
        self._stopping = threading.Event()
        self._pubsub: redis.client.PubSub | None = None
        # Set while Redis is unreachable: when to try subscribing again.
        self._resubscribe_at: float | None = None
        self.logger = logging.getLogger(f"broker.{self.label.lower()}_provider")

    @abstractmethod
    def run_cycle(self) -> int:
        """Fetch what is wanted and write it; return how many keys were written.

        Raises RedisError if Redis can't be written, ending the cycle; a failing device only loses its own keys.
        """

    @abstractmethod
    def interval_s(self) -> float:
        """Return how long the cycles are apart now, in seconds."""

    def stop(self) -> None:
        """Stop after the device being fetched (if any). Safe to call from a signal handler."""
        self._stopping.set()

    def run(self) -> None:
        """Run cycles until stop() is called."""
        self.logger.info("Starting the %s provider", self.label.lower())
        try:
            while not self._stopping.is_set():
                self._try_cycle()
                self._wait_for_next_cycle()
        finally:
            self._close_pubsub()
        self.logger.info("Stopped the %s provider", self.label.lower())

    def _try_cycle(self) -> None:
        """Run a cycle, logging rather than raising if it fails (the next tick tries again)."""
        try:
            self.run_cycle()
        except RedisError as e:
            # The error's type, not its text: a failed pipeline's text quotes its keys and values.
            self.logger.warning("%s: couldn't reach Redis (%s); trying again next tick", self.label, type(e).__name__)
        except Exception:
            # The database, say.
            self.logger.exception("%s: the cycle failed; trying again next tick", self.label)

    def _wait_for_next_cycle(self) -> None:
        """Wait until the next tick, or until a `v1:sub:changed` message or stop() if sooner."""
        due = next_tick(self.clock(), self.interval_s())
        while not self._stopping.is_set():
            remaining = due - self.clock()
            if remaining <= 0 or self._listen(min(remaining, LISTEN_S)):
                return

    def _listen(self, timeout: float) -> bool:
        """Wait up to `timeout` for a `v1:sub:changed` message; return whether one came.

        A message that came during a cycle is already waiting, so the next cycle starts at once.
        """
        if self._pubsub is None and self._resubscribe_at is not None and self.clock() < self._resubscribe_at:
            self._stopping.wait(timeout)
            return False
        try:
            if self._pubsub is None:
                pubsub = self.store.pubsub(ignore_subscribe_messages=True)
                pubsub.subscribe(SUB_CHANGED)
                self._pubsub = pubsub
                if self._resubscribe_at is not None:
                    self.logger.info("%s: listening on %s again", self.label, SUB_CHANGED)
                    self._resubscribe_at = None
            message = self._pubsub.get_message(timeout=timeout)
        except RedisError as e:
            if self._resubscribe_at is None:
                self.logger.warning(
                    "%s: lost %s (%s); subscribing again every %ss",
                    self.label,
                    SUB_CHANGED,
                    type(e).__name__,
                    RESUBSCRIBE_S,
                )
            self._resubscribe_at = self.clock() + RESUBSCRIBE_S
            self._close_pubsub()
            self._stopping.wait(timeout)
            return False
        if message is None or message["type"] != "message":
            return False
        self.logger.debug("%s: subscriptions changed", self.label)
        return True

    def _close_pubsub(self) -> None:
        if self._pubsub is not None:
            with suppress(RedisError):
                self._pubsub.close()
            self._pubsub = None


def read_only_app() -> Flask:
    """Return an app that can only read the broker's database and decrypt what's in it: the one a provider runs in.

    Not broker.create_app(), which migrates the database (taking a lock file next to it) and needs all the broker's
    secrets. This needs only CREDENTIAL_ENCRYPTION_KEY and opens the database read-only (see db.init_read_only_app),
    so a provider can't write to it or hold its write lock.
    """
    app = Flask(__name__)
    app.config["CREDENTIAL_ENCRYPTION_KEY"] = os.environ["CREDENTIAL_ENCRYPTION_KEY"]
    init_read_only_app(app)
    return app


def connect() -> redis.Redis:
    """Return a client for the store, from REDIS_URL and REDIS_PASSWORD."""
    return redis.Redis.from_url(
        os.environ.get("REDIS_URL", "redis://127.0.0.1:6379/0"),
        password=os.environ.get("REDIS_PASSWORD") or None,
        decode_responses=True,
        socket_timeout=5,
        socket_connect_timeout=5,
    )


def configure_logging() -> None:
    """Log JSON lines to stdout, as gunicorn does (gunicorn.conf.py), so Fluent Bit ships them the same way."""
    from broker.log_format import JsonFormatter  # noqa: PLC0415 -- it imports gunicorn, which only this needs here

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO").upper(), handlers=[handler], force=True)


def serve(make_provider: Callable[[Flask, redis.Redis], ProviderLoop]) -> None:
    """Run the provider `make_provider` makes, in read_only_app(), until SIGINT or SIGTERM."""
    configure_logging()
    provider = make_provider(read_only_app(), connect())
    # As a container's PID 1 nothing handles SIGTERM unless we do, and docker stop would have to kill us.
    for signum in (signal.SIGINT, signal.SIGTERM):
        signal.signal(signum, lambda *_: provider.stop())
    provider.run()
