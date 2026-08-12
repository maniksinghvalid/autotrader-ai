"""Engine strategy variants: regime-gated breakout and pullback entries
(Rules 9-12 in docs/BACKTESTING.md). Every reference level must come from
COMPLETED bars only. Dates in 2026 (EST, before DST) per repo convention."""
from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, timezone

import pytest

from autotrader.backtest.data import Bar
from autotrader.backtest.engine import BacktestConfig, run_backtest
from autotrader.config import RiskConfig


def _ts(y, m, d, hh=14, mm=30):
    return int(datetime(y, m, d, hh, mm, tzinfo=timezone.utc).timestamp() * 1000)


def _bar(y, m, d, o, h, l, c, v=1000):
    return Bar(ts=_ts(y, m, d), open=o, high=h, low=l, close=c, volume=v)


def _cfg(**over):
    base = dict(
        symbols=("AAPL",), start=date(2026, 2, 1), end=date(2026, 3, 1),
        multiplier=1, timespan="day", cash=100_000.0,
        commission_per_order=0.0, commission_per_share=0.0, slippage_bps=0.0,
        lookback=3, stop_loss_pct=0.05, take_profit_pct=0.10,
        trailing_stop_pct=0.0, confidence=0.7, order_qty=10,
        strategy="pullback", regime_sma=3, pullback_lookback=3,
        pullback_depth_pct=0.0,
    )
    base.update(over)
    return BacktestConfig(**base)


def _risk():
    return RiskConfig(
        trading_env="PAPER", min_confidence=0.0, max_order_notional=1e9,
        max_position_qty=1_000_000, daily_loss_limit=1e9, max_gross_exposure=1e9,
        allowed_symbols=frozenset({"AAPL"}), risk_per_trade_pct=0.0,
        daily_loss_halt=1e9,
    )


def _uptrend_warmup(days=3, closes=(100, 101, 102), lows=(98, 99, 100)):
    """Completed warmup days in an uptrend: last close (102) > SMA(3) (101),
    ref_low(3) = 98."""
    out = []
    for i in range(days):
        c = closes[i]
        l = lows[i]
        out.append(_bar(2026, 1, 25 + i, c - 0.5, c + 0.5, l, c))
    return out


def _downtrend_warmup(days=3, closes=(102, 101, 98), lows=(100, 99, 96)):
    """Last close (98) < SMA(3) (100.33) -> regime gate closed. ref_low = 96."""
    out = []
    for i in range(days):
        c = closes[i]
        l = lows[i]
        out.append(_bar(2026, 1, 25 + i, c + 0.5, c + 1, l, c))
    return out


# ---------------------------------------------------------------- pullback

def test_pullback_dip_in_uptrend_enters_at_trigger():
    """Rule 11: open above T, low touches T -> fill exactly at T = min(last M lows)."""
    warm = _uptrend_warmup()   # T = 98
    dip = _bar(2026, 2, 2, o=101, h=101.5, l=97.5, c=100)
    result = run_backtest({"AAPL": warm + [dip]}, _cfg(), _risk())
    buys = [f for f in result.fills if f.side == "BUY"]
    assert len(buys) == 1
    assert buys[0].price == pytest.approx(98.0)
    assert buys[0].reason == "pullback"


def test_pullback_open_below_trigger_fills_at_open():
    """Rule 11: a resting buy-limit fills at the open when the open gaps below it."""
    warm = _uptrend_warmup()   # T = 98
    gap_dip = _bar(2026, 2, 2, o=97, h=99, l=96.5, c=98.5)
    result = run_backtest({"AAPL": warm + [gap_dip]}, _cfg(), _risk())
    buys = [f for f in result.fills if f.side == "BUY"]
    assert buys[0].price == pytest.approx(97.0)


def test_pullback_low_above_trigger_no_entry():
    warm = _uptrend_warmup()   # T = 98
    no_dip = _bar(2026, 2, 2, o=101, h=102, l=99, c=100)
    result = run_backtest({"AAPL": warm + [no_dip]}, _cfg(), _risk())
    assert result.fills == ()


def test_pullback_downtrend_regime_blocks_entry():
    """Rule 10: same dip depth, but previous completed close below the SMA."""
    warm = _downtrend_warmup()   # T = 96, regime closed
    deep_dip = _bar(2026, 2, 2, o=97, h=98, l=95, c=96)
    result = run_backtest({"AAPL": warm + [deep_dip]}, _cfg(), _risk())
    assert result.fills == ()


def test_pullback_regime_gate_ignores_current_bar_close():
    """Rule 10 uses the previous COMPLETED close: a current bar whose own
    close would flip the regime must not change the decision."""
    warm = _downtrend_warmup()   # regime closed on completed data
    # current bar dips AND closes very high — if the gate wrongly read the
    # current close, it would open the regime and enter.
    tricky = _bar(2026, 2, 2, o=97, h=200, l=95, c=200)
    result = run_backtest({"AAPL": warm + [tricky]}, _cfg(), _risk())
    buys = [f for f in result.fills if f.side == "BUY"]
    assert buys == []


def test_pullback_depth_form_uses_sma_discount():
    """Rule 9 depth form: T = SMA*(1-depth), not the M-day low."""
    warm = _uptrend_warmup()   # SMA(3) = 101 -> T = 101*0.98 = 98.98
    dip = _bar(2026, 2, 2, o=100, h=100.5, l=98.9, c=100)   # low 98.9 <= 98.98, but > min-low 98
    cfg = _cfg(pullback_depth_pct=0.02)
    result = run_backtest({"AAPL": warm + [dip]}, cfg, _risk())
    buys = [f for f in result.fills if f.side == "BUY"]
    assert len(buys) == 1
    assert buys[0].price == pytest.approx(101 * 0.98)


def test_pullback_insufficient_history_no_entry():
    warm = _uptrend_warmup(days=2, closes=(100, 101), lows=(98, 99))   # < 3 completed days
    dip = _bar(2026, 2, 2, o=100, h=100.5, l=90, c=100)
    result = run_backtest({"AAPL": warm + [dip]}, _cfg(), _risk())
    assert result.fills == ()


def test_pullback_same_bar_stop_fires_take_profit_never():
    """Rule 12 == Rule 6: same-bar stop yes, same-bar take-profit no."""
    warm = _uptrend_warmup()   # T = 98
    # enters at 98, low reaches 98*(1-0.05)=93.1 same bar -> same-bar stop
    crash = _bar(2026, 2, 2, o=101, h=101.5, l=93, c=94)
    result = run_backtest({"AAPL": warm + [crash]}, _cfg(), _risk())
    assert len(result.fills) == 2
    assert result.fills[1].side == "SELL"
    assert result.fills[1].reason == "stop-loss"
    assert result.fills[1].price == pytest.approx(98 * 0.95)

    # enters at 98 and rips through TP same bar -> BUY only, no same-bar TP
    warm2 = _uptrend_warmup()
    rip = _bar(2026, 2, 2, o=101, h=120, l=97.5, c=115)
    result2 = run_backtest({"AAPL": warm2 + [rip]}, _cfg(take_profit_pct=0.10), _risk())
    assert [f.side for f in result2.fills] == ["BUY"]


def test_pullback_exit_via_shared_rules_next_bar():
    warm = _uptrend_warmup()   # entry T = 98
    dip = _bar(2026, 2, 2, o=101, h=101.5, l=97.5, c=100)     # BUY @ 98
    tp_bar = _bar(2026, 2, 3, o=100, h=110, l=99.5, c=108)     # TP = 98*1.10 = 107.8
    result = run_backtest({"AAPL": warm + [dip, tp_bar]}, _cfg(), _risk())
    sells = [f for f in result.fills if f.side == "SELL"]
    assert len(sells) == 1
    assert sells[0].reason == "take-profit"
    assert sells[0].price == pytest.approx(98 * 1.10)


# ---------------------------------------------------------- breakout_regime

def test_breakout_regime_gate_blocks_breakout_below_sma():
    """Same breakout bar enters under plain breakout but not under
    breakout_regime when the regime is closed."""
    warm = _downtrend_warmup(closes=(102, 101, 98), lows=(100, 99, 96))
    # highs are close+1: 103, 102, 99 -> ref_high(3) = 103
    breakout_bar = _bar(2026, 2, 2, o=102, h=104, l=101, c=103)

    plain = run_backtest({"AAPL": warm + [breakout_bar]},
                        _cfg(strategy="breakout"), _risk())
    assert len([f for f in plain.fills if f.side == "BUY"]) == 1

    gated = run_backtest({"AAPL": warm + [breakout_bar]},
                        _cfg(strategy="breakout_regime"), _risk())
    assert gated.fills == ()


def test_breakout_regime_allows_breakout_in_uptrend():
    warm = _uptrend_warmup()   # closes 100,101,102 -> SMA 101, prev close 102 > SMA; ref_high = 102.5
    breakout_bar = _bar(2026, 2, 2, o=102, h=104, l=101.5, c=103.5)
    result = run_backtest({"AAPL": warm + [breakout_bar]},
                         _cfg(strategy="breakout_regime"), _risk())
    buys = [f for f in result.fills if f.side == "BUY"]
    assert len(buys) == 1
    assert buys[0].price == pytest.approx(102.5)   # stop-buy at ref_high


def test_unknown_strategy_raises():
    warm = _uptrend_warmup()
    with pytest.raises(ValueError, match="unknown strategy"):
        run_backtest({"AAPL": warm}, _cfg(strategy="hodl"), _risk())


# --------------------------------------------------------------- indicators

def test_sma_and_ref_low_exclude_current_bar():
    """Day N's entry decision must ignore day N's own low/close: a bar whose
    own low would set a much lower trigger must still trigger off the
    completed-days reference."""
    warm = _uptrend_warmup()   # completed ref_low = 98
    # low goes to 50 (would drag ref_low way down if wrongly included) but the
    # fill must still be at the completed-data trigger 98.
    plunge = _bar(2026, 2, 2, o=101, h=101.5, l=50, c=100)
    result = run_backtest({"AAPL": warm + [plunge]}, _cfg(), _risk())
    buys = [f for f in result.fills if f.side == "BUY"]
    assert buys[0].price == pytest.approx(98.0)


def test_warmup_feeds_lows_and_closes():
    """Bars before cfg.start populate the pullback/SMA indicators (no trades),
    exactly as they do the breakout highs."""
    warm = _uptrend_warmup()   # all before 2026-02-01 -> warmup only
    dip = _bar(2026, 2, 2, o=101, h=101.5, l=97.5, c=100)
    result = run_backtest({"AAPL": warm + [dip]}, _cfg(), _risk())
    assert len([f for f in result.fills if f.side == "BUY"]) == 1


# -------------------------------------------------------------- parity

def test_entry_parity_strategy_class_agrees_with_engine_trigger():
    """The entry rule exists in two places by design (engine trigger for fill
    mechanics, strategy class for production). They must agree when evaluated
    at the same reference values."""
    from autotrader.strategies.pullback import PullbackParams, PullbackStrategy
    from autotrader.strategies.breakout import BreakoutParams, BreakoutStrategy

    pb = PullbackStrategy(PullbackParams("AAPL", 0.05, 0.10, 0.7))
    # engine trigger condition: regime_ok AND price(low) <= T
    assert pb.evaluate(97.5, None, ref_low=98.0, sma=95.0) is not None      # dip + uptrend
    assert pb.evaluate(99.0, None, ref_low=98.0, sma=95.0) is None          # no dip
    assert pb.evaluate(94.0, None, ref_low=98.0, sma=95.0) is None          # dip but downtrend

    bo = BreakoutStrategy(BreakoutParams("AAPL", 0.05, 0.10, 0.7))
    # engine trigger condition: high > ref_high AND (no sma or price > sma)
    assert bo.evaluate(104.0, None, ref_high=103.0, sma=100.0) is not None
    assert bo.evaluate(104.0, None, ref_high=103.0, sma=110.0) is None
