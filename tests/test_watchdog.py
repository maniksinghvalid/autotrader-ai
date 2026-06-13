"""Watchdog: heartbeat + capped exponential backoff. On recovery it runs a
full reconcile BEFORE signalling the loop may resume (research E8). All sleeps
are injected so tests never actually wait."""


def test_backoff_seconds_is_capped_exponential():
    from autotrader.watchdog import backoff_seconds
    assert backoff_seconds(1) == 1.0
    assert backoff_seconds(2) == 2.0
    assert backoff_seconds(3) == 4.0
    assert backoff_seconds(4) == 8.0
    assert backoff_seconds(5) == 16.0
    assert backoff_seconds(6) == 30.0   # capped
    assert backoff_seconds(7) == 30.0   # stays capped


def test_backoff_seconds_floors_attempt_at_one():
    from autotrader.watchdog import backoff_seconds
    assert backoff_seconds(0) == 1.0


def test_ensure_healthy_true_when_first_heartbeat_ok():
    from autotrader.watchdog import Watchdog
    slept, reconciled = [], []
    w = Watchdog(health_check=lambda: True,
                 reconcile=lambda: reconciled.append(1),
                 sleep=lambda s: slept.append(s))
    assert w.ensure_healthy() is True
    assert slept == [], "no backoff when healthy"
    assert reconciled == [], "no reconcile when never lost"


def test_ensure_healthy_recovers_and_reconciles_before_resume():
    from autotrader.watchdog import Watchdog
    # unhealthy, unhealthy, then healthy
    seq = iter([False, False, True])
    slept, reconciled = [], []
    w = Watchdog(health_check=lambda: next(seq),
                 reconcile=lambda: reconciled.append(1),
                 sleep=lambda s: slept.append(s))
    assert w.ensure_healthy() is True
    assert slept == [1.0, 2.0], "backed off twice before recovery"
    assert reconciled == [1], "reconcile ran exactly once, on recovery"


def test_ensure_healthy_false_after_max_attempts_no_reconcile():
    from autotrader.watchdog import Watchdog
    reconciled = []
    w = Watchdog(health_check=lambda: False,
                 reconcile=lambda: reconciled.append(1),
                 sleep=lambda s: None,
                 max_attempts=3)
    assert w.ensure_healthy() is False
    assert reconciled == [], "never reconcile if never recovered"


def test_ensure_healthy_respects_max_attempts_count():
    from autotrader.watchdog import Watchdog
    calls = {"n": 0}

    def health():
        calls["n"] += 1
        return False

    w = Watchdog(health_check=health, reconcile=lambda: None,
                 sleep=lambda s: None, max_attempts=4)
    w.ensure_healthy()
    # 1 initial check + 4 retry checks = 5 calls
    assert calls["n"] == 5
