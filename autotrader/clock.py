"""Wall-clock abstraction in US market time (America/New_York — handles the
EST/EDT transition automatically, unlike a fixed-offset 'EST'). Injecting a
Clock lets the scheduler and watchdog be tested deterministically: production
uses Clock(); tests use FixedClock."""
from __future__ import annotations

from datetime import date, datetime
from typing import FrozenSet
from zoneinfo import ZoneInfo

_NY = ZoneInfo("America/New_York")


def is_trading_day(d: date, holidays: FrozenSet[date] = frozenset()) -> bool:
    """Pure trading-calendar predicate: True on weekdays that are not configured
    full-day market holidays. The caller passes the date (from the injected Clock)
    and the holiday set (from RiskConfig.market_holidays). No SDK, no I/O, no
    clock. Early-close (half-day) awareness is a tracked PRE-LIVE follow-up,
    deliberately not implemented here."""
    return d.weekday() < 5 and d not in holidays


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
