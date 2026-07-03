"""Token bucket rate limiter.

Two instances live in MoomooBroker: one for order submissions
(capacity=15, refill_rate=15/30) and one for refresh-cache queries
(capacity=10, refill_rate=10/30), matching Moomoo API limits.
"""
from __future__ import annotations

import threading
import time


class RateLimiter:
    """Token bucket. acquire() blocks until a token is available or timeout expires.

    capacity:     burst ceiling (tokens)
    refill_rate:  tokens added per second
    """

    def __init__(self, capacity: float, refill_rate: float):
        self._capacity = capacity
        self._refill_rate = refill_rate
        self._tokens = float(capacity)
        self._last = time.monotonic()
        self._lock = threading.Lock()

    def acquire(self, timeout: float = 60.0) -> bool:
        """Consume one token, blocking until available. Returns False on timeout."""
        deadline = time.monotonic() + timeout
        while True:
            with self._lock:
                now = time.monotonic()
                self._tokens = min(
                    self._capacity,
                    self._tokens + (now - self._last) * self._refill_rate,
                )
                self._last = now
                if self._tokens >= 1.0:
                    self._tokens -= 1.0
                    return True
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.02)  # poll at 50 Hz; a full 30 s window has 15 tokens
