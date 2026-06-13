"""Connection watchdog: heartbeat + capped exponential-backoff reconnect.

CLAUDE.md forbids time.sleep() as a *readiness check* — this is exponential
backoff polling, and the sleep function is injected (production passes
time.sleep; tests pass a recorder). On recovery the watchdog runs a full
reconcile BEFORE returning True, so the loop never resumes on stale state
(research E8)."""
from __future__ import annotations

import logging
from typing import Callable

logger = logging.getLogger("autotrader.watchdog")


def backoff_seconds(attempt: int, base: float = 1.0, cap: float = 30.0) -> float:
    """1-based exponential backoff, capped. attempt<=1 -> base."""
    if attempt < 1:
        attempt = 1
    return min(cap, base * (2 ** (attempt - 1)))


class Watchdog:
    def __init__(self, health_check: Callable[[], bool],
                 reconcile: Callable[[], None],
                 sleep: Callable[[float], None],
                 max_attempts: int = 6):
        self._health = health_check
        self._reconcile = reconcile
        self._sleep = sleep
        self._max_attempts = max_attempts

    def ensure_healthy(self) -> bool:
        """True if healthy now, or recovered within max_attempts (a full
        reconcile is run on recovery). False if still unhealthy."""
        if self._health():
            return True
        for attempt in range(1, self._max_attempts + 1):
            delay = backoff_seconds(attempt)
            logger.warning("watchdog: unhealthy — backoff %.1fs (attempt %d/%d)",
                           delay, attempt, self._max_attempts)
            self._sleep(delay)
            if self._health():
                logger.info("watchdog: recovered — full reconcile before resume")
                self._reconcile()
                return True
        logger.error("watchdog: still unhealthy after %d attempts", self._max_attempts)
        return False
