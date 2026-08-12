"""PullbackReference: once-per-day cache, None-not-cached retry, all-or-nothing
legs, kwargs shapes. Mirrors tests/test_breakout_reference.py conventions."""
from __future__ import annotations

from datetime import date

from autotrader.breakout_reference import BreakoutReference, PullbackReference


class _Broker:
    def __init__(self, low=95.0, sma=98.0, high=130.0):
        self.low = low
        self.sma_value = sma
        self.high = high
        self.low_calls = 0
        self.sma_calls = 0

    def recent_low(self, symbol, lookback):
        self.low_calls += 1
        return self.low

    def sma(self, symbol, window):
        self.sma_calls += 1
        return self.sma_value

    def recent_high(self, symbol, lookback):
        return self.high


def test_computed_once_per_day():
    b = _Broker()
    ref = PullbackReference(b, 15, 100, today_fn=lambda: date(2026, 8, 12))
    assert ref.refs("US.AAPL") == (95.0, 98.0)
    assert ref.refs("US.AAPL") == (95.0, 98.0)
    assert b.low_calls == 1 and b.sma_calls == 1


def test_recomputed_on_date_roll():
    b = _Broker()
    days = iter([date(2026, 8, 12), date(2026, 8, 12), date(2026, 8, 13)])
    current = {"d": date(2026, 8, 12)}

    def today():
        return current["d"]

    ref = PullbackReference(b, 15, 100, today_fn=today)
    ref.refs("US.AAPL")
    current["d"] = date(2026, 8, 13)
    ref.refs("US.AAPL")
    assert b.low_calls == 2


def test_none_not_cached_and_retried():
    b = _Broker(low=None)
    ref = PullbackReference(b, 15, 100, today_fn=lambda: date(2026, 8, 12))
    assert ref.refs("US.AAPL") is None
    b.low = 95.0   # data becomes available
    assert ref.refs("US.AAPL") == (95.0, 98.0)


def test_all_or_nothing_either_leg_none_means_none():
    """A missing SMA must never produce a half-gated entry."""
    b = _Broker(low=95.0, sma=None)
    ref = PullbackReference(b, 15, 100, today_fn=lambda: date(2026, 8, 12))
    assert ref.refs("US.AAPL") is None
    assert ref.kwargs("US.AAPL") == {"ref_low": None, "sma": None}


def test_kwargs_shapes_for_both_reference_classes():
    b = _Broker()
    pb = PullbackReference(b, 15, 100, today_fn=lambda: date(2026, 8, 12))
    assert pb.kwargs("US.AAPL") == {"ref_low": 95.0, "sma": 98.0}

    bo = BreakoutReference(b, 20, today_fn=lambda: date(2026, 8, 12))
    assert bo.kwargs("US.AAPL") == {"ref_high": 130.0}
