"""Pure performance math: known-answer fixtures, no engine run required."""
from __future__ import annotations

import math
from datetime import date

import pytest

from autotrader.backtest.engine import BacktestConfig, BacktestResult, Fill
from autotrader.backtest.metrics import (
    RoundTrip, cagr, max_drawdown, pair_round_trips, sharpe, summarize,
)


def _cfg(**over):
    base = dict(symbols=("X",), start=date(2026, 1, 1), end=date(2026, 12, 31),
               multiplier=1, timespan="day", cash=1000.0)
    base.update(over)
    return BacktestConfig(**base)


def test_max_drawdown_known_answer():
    assert max_drawdown([100, 120, 90, 130]) == pytest.approx(0.25)


def test_max_drawdown_monotonic_up_is_zero():
    assert max_drawdown([100, 110, 120]) == 0.0


def test_max_drawdown_empty_is_zero():
    assert max_drawdown([]) == 0.0


def test_sharpe_constant_returns_is_zero_no_zero_division():
    assert sharpe([0.01, 0.01, 0.01]) == 0.0


def test_sharpe_single_value_is_zero():
    assert sharpe([0.01]) == 0.0


def test_sharpe_known_answer():
    # mean=0.01, stdev=sqrt(0.0002)=0.01*sqrt(2) -> mean/stdev = 1/sqrt(2)
    expected = (1 / math.sqrt(2)) * math.sqrt(252)
    assert sharpe([0.02, 0.0]) == pytest.approx(expected)


def test_cagr_known_answer():
    expected = (200.0 / 100.0) ** (365.25 / 365) - 1.0
    assert cagr(100.0, 200.0, 365) == pytest.approx(expected)


def test_cagr_zero_days_is_zero():
    assert cagr(100.0, 200.0, 0) == 0.0


def test_cagr_nonpositive_initial_is_zero():
    assert cagr(0.0, 200.0, 100) == 0.0


def test_pair_round_trips_interleaved_symbols_and_unclosed_excluded():
    fills = [
        Fill(ts=1, symbol="AAPL", side="BUY", qty=10, price=100, commission=1, reason="breakout"),
        Fill(ts=2, symbol="MSFT", side="BUY", qty=5, price=50, commission=1, reason="breakout"),
        Fill(ts=3, symbol="AAPL", side="SELL", qty=10, price=110, commission=1, reason="take-profit"),
        Fill(ts=4, symbol="MSFT", side="SELL", qty=5, price=45, commission=1, reason="stop-loss"),
        Fill(ts=5, symbol="AAPL", side="BUY", qty=10, price=90, commission=1, reason="breakout"),  # unclosed
    ]
    trips = pair_round_trips(fills)
    assert len(trips) == 2
    aapl, msft = trips
    assert aapl.symbol == "AAPL" and aapl.pnl == pytest.approx(98.0)   # (110-100)*10 - 1 - 1
    assert msft.symbol == "MSFT" and msft.pnl == pytest.approx(-27.0)  # (45-50)*5 - 1 - 1


def test_win_rate_profit_factor_avg_from_trips():
    trips = [
        RoundTrip("X", 1, 2, 1, 100, 110, 0, 10.0, "take-profit"),
        RoundTrip("X", 3, 4, 1, 100, 95, 0, -5.0, "stop-loss"),
        RoundTrip("X", 5, 6, 1, 100, 120, 0, 20.0, "take-profit"),
    ]
    result = BacktestResult(
        fills=(), equity_curve=((date(2026, 1, 1), 1025.0, 1025.0),),
        skipped_for_cash=0, cfg=_cfg(), held_days={"X": 1})
    summary = summarize(result, trips)
    p = summary["portfolio"]
    assert p["trade_count"] == 3
    assert p["win_rate"] == pytest.approx(2 / 3)
    assert p["avg_win"] == pytest.approx(15.0)
    assert p["avg_loss"] == pytest.approx(-5.0)
    assert p["profit_factor"] == pytest.approx(30.0 / 5.0)


def test_all_winners_profit_factor_is_none_not_inf():
    trips = [RoundTrip("X", 1, 2, 1, 100, 110, 0, 10.0, "take-profit")]
    result = BacktestResult(
        fills=(), equity_curve=((date(2026, 1, 1), 1010.0, 1010.0),),
        skipped_for_cash=0, cfg=_cfg(), held_days={})
    summary = summarize(result, trips)
    assert summary["portfolio"]["profit_factor"] is None


def test_summarize_handles_zero_trades_without_error():
    result = BacktestResult(
        fills=(), equity_curve=((date(2026, 1, 1), 1000.0, 1000.0),),
        skipped_for_cash=0, cfg=_cfg(), held_days={})
    summary = summarize(result, [])
    assert summary["portfolio"]["trade_count"] == 0
    assert summary["portfolio"]["profit_factor"] is None
    assert summary["portfolio"]["win_rate"] == 0.0


def test_exposure_known_answer():
    curve = (
        (date(2026, 1, 1), 1000.0, 1000.0),   # flat
        (date(2026, 1, 2), 1050.0, 900.0),    # position open (v != c)
        (date(2026, 1, 3), 1040.0, 900.0),    # still open
        (date(2026, 1, 4), 1000.0, 1000.0),   # flat again
    )
    result = BacktestResult(fills=(), equity_curve=curve, skipped_for_cash=0,
                            cfg=_cfg(), held_days={})
    summary = summarize(result, [])
    assert summary["portfolio"]["exposure"] == pytest.approx(2 / 4)


def test_per_symbol_exposure_from_held_days():
    curve = tuple((date(2026, 1, 1 + i), 1000.0, 1000.0) for i in range(5))
    result = BacktestResult(fills=(), equity_curve=curve, skipped_for_cash=0,
                            cfg=_cfg(symbols=("X", "Y")), held_days={"X": 3, "Y": 0})
    summary = summarize(result, [])
    assert summary["per_symbol"]["X"]["exposure"] == pytest.approx(3 / 5)
    assert summary["per_symbol"]["Y"]["exposure"] == pytest.approx(0.0)
