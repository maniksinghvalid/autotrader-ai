"""Token bucket rate limiter tests. No mocks needed — the class is pure Python."""
import time

import pytest


def test_acquire_consumes_token_and_returns_true():
    from autotrader.rate_limiter import RateLimiter
    rl = RateLimiter(capacity=3.0, refill_rate=1.0)
    assert rl.acquire(timeout=0.1) is True


def test_acquire_returns_false_when_drained_and_timeout_expires():
    from autotrader.rate_limiter import RateLimiter
    # capacity=1, refill_rate=0 -> tokens never refill
    rl = RateLimiter(capacity=1.0, refill_rate=0.0)
    assert rl.acquire(timeout=0.5) is True   # consumes the one token
    assert rl.acquire(timeout=0.1) is False  # drained, timeout fires


def test_tokens_refill_over_time():
    from autotrader.rate_limiter import RateLimiter
    # Refill at 200 tokens/sec -> 1 token refills in 0.005s. Drain and wait 0.05s.
    rl = RateLimiter(capacity=1.0, refill_rate=200.0)
    assert rl.acquire(timeout=0.1) is True   # consume the 1 token
    time.sleep(0.05)                          # wait for refill (200/s -> ~0.005s needed)
    assert rl.acquire(timeout=0.1) is True   # should be available again


def test_acquire_is_thread_safe():
    """Concurrent acquires on a capacity-10 limiter must each get exactly one token."""
    import threading
    from autotrader.rate_limiter import RateLimiter

    rl = RateLimiter(capacity=10.0, refill_rate=0.0)  # no refill — 10 tokens total
    results = []
    lock = threading.Lock()

    def grab():
        got = rl.acquire(timeout=0.5)
        with lock:
            results.append(got)

    threads = [threading.Thread(target=grab) for _ in range(15)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # Exactly 10 acquires succeed; 5 time out.
    assert results.count(True) == 10
    assert results.count(False) == 5
