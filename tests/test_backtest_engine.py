"""Bar-walking simulator: each test pins one numbered rule from engine.py's
docstring / docs/BACKTESTING.md. Dates in 2026 per repo convention (all in
EST, before the March DST switch, so ts->ET date conversions never cross
midnight for our fixed hh=14:30 UTC default).

Entry bars use open <= ref_high (100) unless deliberately testing a gap-up,
so the resulting position's avg_price is exactly 100 and SL/TP/trailing
levels are easy hand-computed numbers."""
from __future__ import annotations

from datetime import date, datetime, timezone

import pytest

from autotrader.backtest.data import Bar
from autotrader.backtest.engine import BacktestConfig, run_backtest
from autotrader.config import RiskConfig


def _ts(y, m, d, hh=14, mm=30):
    return int(datetime(y, m, d, hh, mm, tzinfo=timezone.utc).timestamp() * 1000)


def _bar(y, m, d, o, h, l, c, v=1000, hh=14, mm=30):
    return Bar(ts=_ts(y, m, d, hh, mm), open=o, high=h, low=l, close=c, volume=v)


def _cfg(**over):
    base = dict(
        symbols=("AAPL",), start=date(2026, 2, 1), end=date(2026, 3, 1),
        multiplier=1, timespan="day", cash=100_000.0,
        commission_per_order=0.0, commission_per_share=0.0, slippage_bps=0.0,
        lookback=3, stop_loss_pct=0.05, take_profit_pct=0.10,
        trailing_stop_pct=0.0, confidence=0.7, order_qty=10,
    )
    base.update(over)
    return BacktestConfig(**base)


def _permissive_risk(**over):
    base = dict(
        trading_env="PAPER", min_confidence=0.0, max_order_notional=1e9,
        max_position_qty=1_000_000, daily_loss_limit=1e9, max_gross_exposure=1e9,
        allowed_symbols=frozenset({"AAPL", "MSFT"}), risk_per_trade_pct=0.0,
        daily_loss_halt=1e9,
    )
    base.update(over)
    return RiskConfig(**base)


def _warmup_bars(lookback=3, start_day=25, month=1, highs=None):
    """`lookback` flat warmup bars (no breakout) ending the day before Feb 1."""
    highs = highs or [100.0] * lookback
    out = []
    day = start_day
    for h in highs:
        out.append(_bar(2026, month, day, 99, h, 98, 99.5))
        day += 1
    return out


# entry bar with open <= ref_high(100) -> fills exactly at 100, avg_price == 100
def _entry_bar(day=1, h=102, l=99, c=101.5, o=99.5):
    return _bar(2026, 2, day, o, h, l, c)


def test_rule5_stop_buy_fills_at_ref_high_when_no_gap():
    """h > ref_high, o < ref_high -> BUY fills at ref_high (the trigger), not open."""
    bars = _warmup_bars() + [_entry_bar()]
    result = run_backtest({"AAPL": bars}, _cfg(), _permissive_risk())
    buys = [f for f in result.fills if f.side == "BUY"]
    assert len(buys) == 1
    assert buys[0].price == pytest.approx(100.0)
    assert buys[0].reason == "breakout"


def test_rule5_gap_up_entry_fills_at_open_not_reference():
    entry = _bar(2026, 2, 1, o=105, h=106, l=104.5, c=105.5)   # gapped above ref_high
    bars = _warmup_bars() + [entry]
    result = run_backtest({"AAPL": bars}, _cfg(), _permissive_risk())
    buys = [f for f in result.fills if f.side == "BUY"]
    assert buys[0].price == pytest.approx(105.0)


def test_strict_inequality_equal_high_does_not_enter():
    entry = _bar(2026, 2, 1, o=99, h=100, l=98, c=99.5)   # h == ref_high exactly
    bars = _warmup_bars() + [entry]
    result = run_backtest({"AAPL": bars}, _cfg(), _permissive_risk())
    assert result.fills == ()


def test_no_entry_until_lookback_completed_days_exist():
    warm = _warmup_bars(highs=[100, 100])   # only 2, lookback=3
    bars = warm + [_entry_bar()]
    result = run_backtest({"AAPL": bars}, _cfg(lookback=3), _permissive_risk())
    assert result.fills == ()


def test_warmup_bars_feed_ref_high_but_never_trade():
    """A breakout inside the warmup window (before cfg.start) must not trade."""
    bars = [
        _bar(2026, 1, 25, 99, 100, 98, 99.5),
        _bar(2026, 1, 26, 100.5, 105, 100, 104),   # would breakout, but still warmup
        _bar(2026, 1, 27, 99, 100, 98, 99.5),
        _bar(2026, 2, 1, 99, 99.5, 98, 99),   # trading starts; no breakout this bar
    ]
    result = run_backtest({"AAPL": bars}, _cfg(lookback=3), _permissive_risk())
    assert result.fills == ()


def test_forming_bar_excluded_from_its_own_ref_high():
    """A bar cannot use its own high as the reference it beats (no self-lookahead)."""
    entry = _entry_bar(h=150)   # huge high, but that's THIS bar
    bars = _warmup_bars() + [entry]
    result = run_backtest({"AAPL": bars}, _cfg(), _permissive_risk())
    buys = [f for f in result.fills if f.side == "BUY"]
    assert len(buys) == 1
    assert buys[0].price == pytest.approx(100.0)   # off yesterday's ref, not its own 150 high


def test_no_reentry_while_held():
    day1 = _entry_bar(day=1)                                     # enters
    day2 = _bar(2026, 2, 2, o=101.5, h=110, l=101, c=105)         # new high, but already held
    bars = _warmup_bars() + [day1, day2]
    result = run_backtest({"AAPL": bars}, _cfg(), _permissive_risk())
    buys = [f for f in result.fills if f.side == "BUY"]
    assert len(buys) == 1


def test_rule1_open_gap_stop_loss_fills_at_open():
    entry = _entry_bar(day=1)                              # BUY @ 100
    gap_down = _bar(2026, 2, 2, o=90, h=91, l=89, c=90)     # opens below stop
    bars = _warmup_bars() + [entry, gap_down]
    result = run_backtest({"AAPL": bars}, _cfg(), _permissive_risk())
    sells = [f for f in result.fills if f.side == "SELL"]
    assert len(sells) == 1
    assert sells[0].price == pytest.approx(90.0)
    assert sells[0].reason == "stop-loss"


def test_rule2_intrabar_stop_loss_exact_price():
    entry = _entry_bar(day=1)                                  # BUY @ 100 -> stop_loss @ 95
    drop = _bar(2026, 2, 2, o=99, h=99.5, l=94, c=98)          # low breaches 95 intrabar
    bars = _warmup_bars() + [entry, drop]
    result = run_backtest({"AAPL": bars}, _cfg(), _permissive_risk())
    sells = [f for f in result.fills if f.side == "SELL"]
    assert sells[0].price == pytest.approx(95.0)
    assert sells[0].reason == "stop-loss"


def test_rule4_intrabar_take_profit_exact_price():
    entry = _entry_bar(day=1)                                  # BUY @ 100 -> take_profit @ 110
    rip = _bar(2026, 2, 2, o=101, h=112, l=100.5, c=105)
    bars = _warmup_bars() + [entry, rip]
    result = run_backtest({"AAPL": bars}, _cfg(), _permissive_risk())
    sells = [f for f in result.fills if f.side == "SELL"]
    assert sells[0].price == pytest.approx(110.0)
    assert sells[0].reason == "take-profit"


def test_sl_and_tp_same_bar_stop_loss_wins():
    entry = _entry_bar(day=1)                                  # BUY @ 100
    wild = _bar(2026, 2, 2, o=100, h=120, l=90, c=100)          # both SL(95) and TP(110) reachable
    bars = _warmup_bars() + [entry, wild]
    result = run_backtest({"AAPL": bars}, _cfg(), _permissive_risk())
    sells = [f for f in result.fills if f.side == "SELL"]
    assert sells[0].reason == "stop-loss"
    assert sells[0].price == pytest.approx(95.0)


def test_trailing_daily_reseed_overnight_gap_down_alone_does_not_fire():
    """Fixed stop-loss disabled so only trailing's reseed behavior is under
    test: an overnight gap down, even one bigger than the trail%, must NOT
    fire the trailing stop by itself — it reseeds to the new day's own open."""
    entry = _entry_bar(day=1)                                  # BUY @ 100
    gap_down_but_flat = _bar(2026, 2, 2, o=80, h=80.5, l=79, c=80)
    cfg = _cfg(trailing_stop_pct=2.0, stop_loss_pct=0.99, take_profit_pct=0.99)
    bars = _warmup_bars() + [entry, gap_down_but_flat]
    result = run_backtest({"AAPL": bars}, cfg, _permissive_risk())
    assert [f for f in result.fills if f.side == "SELL"] == []


def test_trailing_daily_reseed_intraday_drop_from_open_fires():
    """stop_loss_pct kept far away (0.99) so only the trailing trigger can
    breach: today's own open sets the trail (daily reseed), and an intrabar
    drop through it fires."""
    entry = _entry_bar(day=1)
    drop = _bar(2026, 2, 2, o=100, h=100.5, l=96, c=97)   # trail = 100*(1-0.02) = 98
    cfg = _cfg(trailing_stop_pct=2.0, stop_loss_pct=0.99)
    bars = _warmup_bars() + [entry, drop]
    result = run_backtest({"AAPL": bars}, cfg, _permissive_risk())
    sells = [f for f in result.fills if f.side == "SELL"]
    assert len(sells) == 1
    assert sells[0].reason == "trailing-stop"
    assert sells[0].price == pytest.approx(98.0)


def test_trailing_prior_day_peak_not_carried_daily_reseed():
    """A high day-2 peak then a day-3 pullback that would breach the OLD
    peak's trail but stays above the NEW day's own open-based trail must NOT
    fire — proves the high-water mark does not carry across the reseed."""
    entry = _entry_bar(day=1)                                     # BUY @ 100
    spike = _bar(2026, 2, 2, o=101, h=108, l=100.5, c=107)         # peak day; stale trail would be 108*0.95=102.6
    pullback = _bar(2026, 2, 3, o=100, h=101, l=96, c=99)          # own trail = 100*0.95=95; stale trail=102.6 (96<=102.6 would wrongly fire)
    cfg = _cfg(trailing_stop_pct=5.0, stop_loss_pct=0.99, take_profit_pct=0.99)
    bars = _warmup_bars() + [entry, spike, pullback]
    result = run_backtest({"AAPL": bars}, cfg, _permissive_risk())
    assert [f for f in result.fills if f.side == "SELL"] == []


def test_rule3_daily_close_below_high_trail_fires():
    entry = _entry_bar(day=1)                                  # BUY @ 100
    # rose to 150 then fell to close 140; low(97) never breaches either
    # intrabar trigger, but close <= high*(1-trail) = 142.5
    rose_then_fell = _bar(2026, 2, 2, o=101, h=150, l=97, c=140)
    cfg = _cfg(trailing_stop_pct=5.0)
    bars = _warmup_bars() + [entry, rose_then_fell]
    result = run_backtest({"AAPL": bars}, cfg, _permissive_risk())
    sells = [f for f in result.fills if f.side == "SELL"]
    assert len(sells) == 1
    assert sells[0].reason == "trailing-stop"
    assert sells[0].price == pytest.approx(150 * 0.95)


def test_stop_loss_vs_trailing_higher_trigger_wins():
    entry = _entry_bar(day=1)                                  # BUY @ 100; SL trigger = 95
    # trailing_stop_pct=2 -> today's own-open trigger = 98*0.98=96.04, higher than SL(95)
    drop = _bar(2026, 2, 2, o=98, h=98.5, l=93, c=94)
    cfg = _cfg(trailing_stop_pct=2.0)
    bars = _warmup_bars() + [entry, drop]
    result = run_backtest({"AAPL": bars}, cfg, _permissive_risk())
    sells = [f for f in result.fills if f.side == "SELL"]
    assert sells[0].price == pytest.approx(96.04)
    assert sells[0].reason == "trailing-stop"


def test_rule6_same_bar_entry_then_stop_loss_fires():
    entry = _entry_bar(day=1, h=102, l=95, c=96)   # breaks out at 100, then crashes same bar
    bars = _warmup_bars() + [entry]
    result = run_backtest({"AAPL": bars}, _cfg(), _permissive_risk())
    assert len(result.fills) == 2
    buy, sell = result.fills
    assert buy.side == "BUY" and buy.price == pytest.approx(100.0)
    assert sell.side == "SELL" and sell.reason == "stop-loss"
    assert sell.price == pytest.approx(100.0 * 0.95)


def test_rule6_same_bar_entry_take_profit_never_taken():
    entry = _entry_bar(day=1, h=115, l=99, c=112)   # breaks out AND would hit TP same bar
    bars = _warmup_bars() + [entry]
    result = run_backtest({"AAPL": bars}, _cfg(), _permissive_risk())
    assert len(result.fills) == 1
    assert result.fills[0].side == "BUY"


def test_exit_bar_cannot_reenter_same_bar():
    entry = _entry_bar(day=1)
    # this bar stops out AND has a high that would otherwise breakout again
    exit_bar = _bar(2026, 2, 2, o=90, h=200, l=89, c=90)
    bars = _warmup_bars() + [entry, exit_bar]
    result = run_backtest({"AAPL": bars}, _cfg(), _permissive_risk())
    assert len(result.fills) == 2
    assert result.fills[1].side == "SELL"


def test_multisymbol_exit_frees_cash_for_same_bar_entry_and_cash_never_negative():
    cfg = _cfg(symbols=("AAPL", "MSFT"), cash=10_500.0, order_qty=100)
    warm_a = _warmup_bars()
    warm_m = [_bar(2026, 1, 25 + i, 99, 100, 98, 99.5) for i in range(3)]
    aapl_entry = _entry_bar(day=1)                                          # AAPL enters day1
    aapl_exit = _bar(2026, 2, 2, o=90, h=90.5, l=89, c=90)                  # AAPL stops out (frees cash)
    msft_no_break = _bar(2026, 2, 1, 99, 99.5, 98, 99)                      # MSFT no breakout day1
    msft_entry = _bar(2026, 2, 2, o=99.5, h=102, l=99, c=101)               # MSFT breaks out SAME bar as AAPL exit
    bars = {
        "AAPL": warm_a + [aapl_entry, aapl_exit],
        "MSFT": warm_m + [msft_no_break, msft_entry],
    }
    result = run_backtest(bars, cfg, _permissive_risk())
    aapl_fills = [f for f in result.fills if f.symbol == "AAPL"]
    msft_fills = [f for f in result.fills if f.symbol == "MSFT"]
    assert len(aapl_fills) == 2 and aapl_fills[1].side == "SELL"
    assert len(msft_fills) == 1 and msft_fills[0].side == "BUY"
    running = cfg.cash
    for f in sorted(result.fills, key=lambda x: x.ts):
        running += -(f.qty * f.price + f.commission) if f.side == "BUY" else (f.qty * f.price - f.commission)
        assert running >= -1e-6


def test_sizing_parity_with_direct_size_position_call():
    from autotrader.sizing import size_position
    risk_cfg = _permissive_risk(risk_per_trade_pct=0.01, confidence_size_floor=0.5,
                                confidence_size_ceil=1.0, trailing_stop_pct=5.0)
    entry = _entry_bar(day=1)
    bars = _warmup_bars() + [entry]
    cfg = _cfg(trailing_stop_pct=5.0, order_qty=1)
    result = run_backtest({"AAPL": bars}, cfg, risk_cfg)
    buy = [f for f in result.fills if f.side == "BUY"][0]
    expected = size_position(equity=cfg.cash, entry_price=100.0, signal_stop=None,
                             confidence=cfg.confidence, cfg=risk_cfg, current_qty=0,
                             gross_exposure=0.0, fixed_qty=cfg.order_qty)
    assert buy.qty == expected.qty


def test_fixed_mode_notional_clamp_applies():
    """FIXED_DISABLED sizing is uncapped by size_position itself — the engine
    must still clamp to max_order_notional."""
    entry = _entry_bar(day=1)
    bars = _warmup_bars() + [entry]
    risk_cfg = _permissive_risk(max_order_notional=500.0)
    result = run_backtest({"AAPL": bars}, _cfg(order_qty=1000), risk_cfg)
    buy = [f for f in result.fills if f.side == "BUY"][0]
    assert buy.qty * buy.price <= 500.0 + 1e-9


def test_no_look_ahead_prefix_property():
    """Fills through day D must be IDENTICAL whether the run stops at D or
    continues further — nothing after D may influence an earlier decision."""
    full = _warmup_bars() + [
        _entry_bar(day=1),                                   # enters
        _bar(2026, 2, 2, 101, 101.5, 99, 100),
        _bar(2026, 2, 3, 100, 130, 99, 128),                  # takes profit
        _bar(2026, 2, 4, 99, 100, 97, 98),
        _bar(2026, 2, 5, 98, 105, 97, 104),                   # would re-enter
    ]
    truncated = full[:-2]   # cut off after Feb 3
    cfg = _cfg()
    result_full = run_backtest({"AAPL": full}, cfg, _permissive_risk())
    result_trunc = run_backtest({"AAPL": truncated}, cfg, _permissive_risk())
    cutoff_ts = truncated[-1].ts
    fills_full_prefix = [f for f in result_full.fills if f.ts <= cutoff_ts]
    assert fills_full_prefix == list(result_trunc.fills)


def test_conservation_final_equity_matches_cash_plus_positions():
    bars = _warmup_bars() + [
        _entry_bar(day=1),
        _bar(2026, 2, 2, 101, 130, 100, 128),   # takes profit
    ]
    cfg = _cfg(commission_per_order=1.0, slippage_bps=10.0)
    result = run_backtest({"AAPL": bars}, cfg, _permissive_risk())
    running = cfg.cash
    for f in result.fills:
        running += -(f.qty * f.price + f.commission) if f.side == "BUY" else (f.qty * f.price - f.commission)
    assert result.equity_curve[-1][1] == pytest.approx(running, abs=1e-6)
    assert result.equity_curve[-1][2] == pytest.approx(running, abs=1e-6)   # flat -> cash == equity


def test_intraday_entry_window_blocks_premarket_bar():
    cfg = _cfg(timespan="minute", multiplier=5)
    warm = [_bar(2026, 1, 25 + i, 99, 100, 98, 99.5, hh=14, mm=0) for i in range(3)]
    premarket = _bar(2026, 2, 2, 100.5, 102, 100, 101.5, hh=13, mm=0)   # 08:00 ET
    bars = warm + [premarket]
    result = run_backtest({"AAPL": bars}, cfg, _permissive_risk())
    assert result.fills == ()


def test_intraday_entry_window_allows_mid_session_bar():
    cfg = _cfg(timespan="minute", multiplier=5)
    warm = [_bar(2026, 1, 25 + i, 99, 100, 98, 99.5, hh=14, mm=0) for i in range(3)]
    mid_session = _bar(2026, 2, 2, 100.5, 102, 100, 101.5, hh=15, mm=30)   # ~10:30 ET
    bars = warm + [mid_session]
    result = run_backtest({"AAPL": bars}, cfg, _permissive_risk())
    buys = [f for f in result.fills if f.side == "BUY"]
    assert len(buys) == 1
