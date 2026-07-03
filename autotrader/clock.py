"""Wall-clock abstraction in US market time (America/New_York — handles the
EST/EDT transition automatically, unlike a fixed-offset 'EST'). Injecting a
Clock lets the scheduler and watchdog be tested deterministically: production
uses Clock(); tests use FixedClock."""
from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

_NY = ZoneInfo("America/New_York")


class Clock:
    """Real wall-clock in market time."""

    def now_est(self) -> datetime:
        return datetime.now(_NY)


class FixedClock(Clock):
    """Deterministic clock for tests. Holds a single tz-aware datetime."""

    def __init__(self, dt: datetime):
        self._dt = dt

    def now_est(self) -> datetime:
        return self._dt

    def set(self, dt: datetime) -> None:
        self._dt = dt
