from datetime import date
from autotrader.breakout_reference import BreakoutReference


class _Broker:
    def __init__(self, values):
        self.values = list(values)   # popped per call
        self.calls = 0

    def recent_high(self, symbol, lookback):
        self.calls += 1
        return self.values.pop(0) if self.values else None


def test_fetches_once_per_day_then_caches():
    b = _Broker([130.5])
    ref = BreakoutReference(b, 20, today_fn=lambda: date(2026, 7, 2))
    assert ref.high("US.AAPL") == 130.5
    assert ref.high("US.AAPL") == 130.5   # cache hit, no second fetch
    assert b.calls == 1


def test_recomputes_on_date_roll():
    b = _Broker([130.5, 131.9])
    day = {"d": date(2026, 7, 2)}
    ref = BreakoutReference(b, 20, today_fn=lambda: day["d"])
    assert ref.high("US.AAPL") == 130.5
    day["d"] = date(2026, 7, 3)
    assert ref.high("US.AAPL") == 131.9
    assert b.calls == 2


def test_none_is_not_cached_and_retried():
    b = _Broker([None, 130.5])
    ref = BreakoutReference(b, 20, today_fn=lambda: date(2026, 7, 2))
    assert ref.high("US.AAPL") is None    # first fetch fails
    assert ref.high("US.AAPL") == 130.5   # retried same day, now succeeds
    assert b.calls == 2


def test_lookback_clamped_to_at_least_one():
    b = _Broker([130.5])
    ref = BreakoutReference(b, 0, today_fn=lambda: date(2026, 7, 2))
    ref.high("US.AAPL")
    assert ref._lookback == 1
