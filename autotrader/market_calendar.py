"""Pure trading-calendar predicate (spec W3). No SDK, no I/O, no clock — the
caller passes the date (from the injected America/New_York Clock) and the
holiday set (from RiskConfig.market_holidays). Early-close (half-day)
awareness is a tracked PRE-LIVE follow-up, deliberately not implemented here."""
from __future__ import annotations

from datetime import date
from typing import FrozenSet


def is_trading_day(d: date, holidays: FrozenSet[date] = frozenset()) -> bool:
    """True on weekdays that are not configured full-day market holidays."""
    return d.weekday() < 5 and d not in holidays
