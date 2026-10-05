"""Throttles guessing at /pair: each client IP gets a limited number of failed codes a minute,
and each time it hits that limit it's locked out for twice as long as the time before. /register
uses a second instance with its own limit, recording every call rather than only failures.

State lives in memory, so it's per process and resets on restart. That fits the single gunicorn
worker the broker runs with; several workers would each keep their own count."""

import threading
import time
from collections import deque
from dataclasses import dataclass, field

MAX_FAILURES = 10
WINDOW_SECONDS = 60.0
# /register's limit: a device registers once (or once a boot), so a few a minute is plenty.
REGISTRATIONS_PER_MINUTE = 5
# Lockouts run 1, 2, 4, ... minutes, capped here; a client that stays quiet this long after
# its last lockout ends starts again from one minute.
MAX_LOCKOUT_SECONDS = 60 * 60.0


@dataclass
class _Client:
    failures: deque[float] = field(default_factory=deque)
    lockouts: int = 0
    locked_until: float = 0.0


class PairThrottle:
    def __init__(self, clock=time.monotonic, max_events: int = MAX_FAILURES, window: float = WINDOW_SECONDS):
        self._clock = clock
        self._max_events = max_events
        self._window = window
        self._clients: dict[str, _Client] = {}
        self._lock = threading.Lock()
        self._last_prune = clock()

    def retry_after(self, ip: str) -> int | None:
        """Whole seconds until this IP may try again, or None if it isn't locked out."""
        with self._lock:
            client = self._clients.get(ip)
            remaining = client.locked_until - self._clock() if client else 0
            return max(1, round(remaining)) if remaining > 0 else None

    def record_failure(self, ip: str) -> int | None:
        """Counts a wrong code from this IP. Returns the lockout in seconds if this failure
        triggered one, otherwise None."""
        with self._lock:
            now = self._clock()
            self._prune(now)
            client = self._clients.setdefault(ip, _Client())
            if client.lockouts and now - client.locked_until > MAX_LOCKOUT_SECONDS:
                client.lockouts = 0

            client.failures.append(now)
            while client.failures[0] <= now - self._window:
                client.failures.popleft()
            if len(client.failures) < self._max_events:
                return None

            lockout = min(self._window * 2**client.lockouts, MAX_LOCKOUT_SECONDS)
            client.lockouts += 1
            client.locked_until = now + lockout
            client.failures.clear()
            return round(lockout)

    def _prune(self, now: float) -> None:
        """Drops clients with nothing left worth remembering, at most once a window, so the
        table doesn't grow with every IP that ever got a code wrong."""
        if now - self._last_prune < self._window:
            return
        self._last_prune = now
        for ip, client in list(self._clients.items()):
            stale_failures = not client.failures or client.failures[-1] <= now - self._window
            lockouts_forgotten = not client.lockouts or now - client.locked_until > MAX_LOCKOUT_SECONDS
            if stale_failures and lockouts_forgotten:
                del self._clients[ip]
