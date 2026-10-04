from broker.services import pair_throttle
from broker.services.pair_throttle import MAX_FAILURES, MAX_LOCKOUT_SECONDS, WINDOW_SECONDS, PairThrottle


class FakeClock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


def _fail(throttle, ip, times):
    """Records `times` failures and returns the last result."""
    result = None
    for _ in range(times):
        result = throttle.record_failure(ip)
    return result


def test_failures_under_the_limit_never_lock_out():
    throttle = PairThrottle(clock=FakeClock())

    assert _fail(throttle, "1.2.3.4", MAX_FAILURES - 1) is None
    assert throttle.retry_after("1.2.3.4") is None


def test_hitting_the_limit_locks_out_for_a_minute():
    clock = FakeClock()
    throttle = PairThrottle(clock=clock)

    assert _fail(throttle, "1.2.3.4", MAX_FAILURES) == 60
    assert throttle.retry_after("1.2.3.4") == 60

    clock.advance(59.5)
    assert throttle.retry_after("1.2.3.4") == 1  # never rounds down to "try again in 0 seconds"
    clock.advance(0.5)
    assert throttle.retry_after("1.2.3.4") is None


def test_failures_spread_over_more_than_a_window_never_lock_out():
    clock = FakeClock()
    throttle = PairThrottle(clock=clock)

    for _ in range(MAX_FAILURES * 3):
        assert throttle.record_failure("1.2.3.4") is None
        clock.advance(WINDOW_SECONDS / (MAX_FAILURES - 1))


def test_each_lockout_doubles_up_to_the_cap():
    clock = FakeClock()
    throttle = PairThrottle(clock=clock)

    lockouts = []
    for _ in range(9):
        lockout = _fail(throttle, "1.2.3.4", MAX_FAILURES)
        lockouts.append(lockout)
        clock.advance(lockout)

    assert lockouts == [60, 120, 240, 480, 960, 1920, 3600, 3600, 3600]


def test_lockouts_start_over_after_a_quiet_spell():
    clock = FakeClock()
    throttle = PairThrottle(clock=clock)
    clock.advance(_fail(throttle, "1.2.3.4", MAX_FAILURES))
    clock.advance(_fail(throttle, "1.2.3.4", MAX_FAILURES))

    clock.advance(MAX_LOCKOUT_SECONDS + 1)

    assert _fail(throttle, "1.2.3.4", MAX_FAILURES) == 60


def test_ips_are_counted_separately():
    throttle = PairThrottle(clock=FakeClock())

    _fail(throttle, "1.2.3.4", MAX_FAILURES)

    assert throttle.retry_after("1.2.3.4") is not None
    assert throttle.retry_after("5.6.7.8") is None
    assert throttle.record_failure("5.6.7.8") is None


def test_forgets_ips_with_nothing_left_to_remember():
    clock = FakeClock()
    throttle = PairThrottle(clock=clock)
    throttle.record_failure("1.2.3.4")
    _fail(throttle, "5.6.7.8", MAX_FAILURES)

    clock.advance(WINDOW_SECONDS + 1)
    throttle.record_failure("9.9.9.9")
    # 1.2.3.4's failure is too old to count, but 5.6.7.8's lockout could still grow if it comes back.
    assert set(throttle._clients) == {"5.6.7.8", "9.9.9.9"}

    clock.advance(MAX_LOCKOUT_SECONDS + WINDOW_SECONDS + 1)
    throttle.record_failure("9.9.9.9")
    assert set(throttle._clients) == {"9.9.9.9"}


def test_uses_the_monotonic_clock_by_default():
    assert PairThrottle()._clock is pair_throttle.time.monotonic
