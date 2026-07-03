"""Unit tests for the pure risk-per-trade sizing core (autotrader/sizing.py).

size_position is stateless and SDK-free: equity + price + stop + confidence in,
an integer share quantity out. It NEVER approves an order (risk_core does that);
it only proposes a size and clamps it DOWN to cap headroom.
"""
import math

import pytest

from autotrader.config import RiskConfig
from autotrader.sizing import size_position, SizeResult


def _cfg(**over):
    base = dict(trading_env="PAPER", min_confidence=0.6, max_order_notional=1e9,
                max_position_qty=10000, daily_loss_limit=500, max_gross_exposure=1e9,
                allowed_symbols=frozenset({"US.AAPL"}), risk_per_trade_pct=0.01)
    base.update(over)
    return RiskConfig(**base)


def _size(**over):
    base = dict(equity=100000.0, entry_price=100.0, signal_stop=98.0, confidence=1.0,
                cfg=_cfg(), current_qty=0, gross_exposure=0.0, fixed_qty=1)
    base.update(over)
    return size_position(**base)


def test_disabled_returns_fixed_qty():
    r = _size(cfg=_cfg(risk_per_trade_pct=0.0), fixed_qty=7)
    assert r == SizeResult(7, "FIXED_DISABLED", False)


def test_basic_risk_size_known_numbers():
    # equity 100k, risk 1% = $1000; stop_dist = 100-98 = $2; base = 500; conf 1.0 -> 500.
    r = _size(confidence=1.0)
    assert r.used_risk_sizing is True and r.reason == "SIZED" and r.qty == 500


def test_confidence_floor_at_min_confidence():
    # confidence == min_confidence -> factor 0.5 -> 500 * 0.5 = 250.
    r = _size(confidence=0.6)
    assert r.qty == 250


def test_confidence_midpoint():
    # halfway between 0.6 and 1.0 is 0.8 -> factor 0.75 -> 500 * 0.75 = 375.
    r = _size(confidence=0.8)
    assert r.qty == 375


def test_confidence_ceiling_at_one():
    r = _size(confidence=1.0)
    assert r.qty == 500


def test_signal_stop_priority_over_trailing():
    # signal_stop 98 (dist 2) wins over trailing 5% (dist 5) -> base 500, not 200.
    r = _size(signal_stop=98.0, cfg=_cfg(trailing_stop_pct=5.0), confidence=1.0)
    assert r.qty == 500


def test_trailing_fallback_when_no_signal_stop():
    # no signal stop, trailing 5% -> stop_dist = 100*0.05 = 5 -> base = 1000/5 = 200.
    r = _size(signal_stop=None, cfg=_cfg(trailing_stop_pct=5.0), confidence=1.0)
    assert r.qty == 200 and r.reason == "SIZED"


def test_no_stop_at_all_falls_back_to_fixed():
    r = _size(signal_stop=None, cfg=_cfg(trailing_stop_pct=0.0), fixed_qty=3)
    assert r == SizeResult(3, "NO_STOP_DISTANCE_FALLBACK", False)


def test_stop_above_or_at_entry_is_ignored():
    # signal_stop >= entry can't define a long stop distance; fall back (here to fixed).
    r = _size(signal_stop=100.0, cfg=_cfg(trailing_stop_pct=0.0), fixed_qty=4)
    assert r.qty == 4 and r.reason == "NO_STOP_DISTANCE_FALLBACK"
    r2 = _size(signal_stop=120.0, cfg=_cfg(trailing_stop_pct=0.0), fixed_qty=4)
    assert r2.qty == 4 and r2.reason == "NO_STOP_DISTANCE_FALLBACK"


def test_stop_above_entry_falls_through_to_trailing():
    # invalid signal stop, but trailing is set -> use trailing, not the bad stop.
    r = _size(signal_stop=130.0, cfg=_cfg(trailing_stop_pct=5.0), confidence=1.0)
    assert r.qty == 200 and r.reason == "SIZED"


def test_zero_equity_returns_zero():
    r = _size(equity=0.0)
    assert r == SizeResult(0, "SIZED_ZERO_BAD_INPUTS", True)


def test_negative_equity_returns_zero():
    assert _size(equity=-5.0).reason == "SIZED_ZERO_BAD_INPUTS"


def test_fractional_floor_to_zero():
    # risk_capital tiny vs stop_dist -> base floors to 0 -> SIZED_ZERO.
    r = _size(cfg=_cfg(risk_per_trade_pct=0.00001), equity=100.0)
    assert r == SizeResult(0, "SIZED_ZERO", True)


def test_clamp_max_position_qty_headroom():
    # base would be 500 but only 30 shares of headroom remain.
    r = _size(current_qty=9970, cfg=_cfg(max_position_qty=10000))
    assert r.qty == 30


def test_clamp_max_order_notional():
    # notional cap 1000 at price 100 -> at most 10 shares.
    r = _size(cfg=_cfg(max_order_notional=1000.0))
    assert r.qty == 10


def test_clamp_gross_exposure_headroom():
    # only $500 of gross-exposure headroom at price 100 -> 5 shares.
    r = _size(cfg=_cfg(max_gross_exposure=10000.0), gross_exposure=9500.0)
    assert r.qty == 5


def test_full_exposure_clamps_to_zero():
    r = _size(cfg=_cfg(max_gross_exposure=10000.0), gross_exposure=10000.0)
    assert r == SizeResult(0, "SIZED_ZERO", True)


def test_clamp_never_raises_above_base():
    # generous caps -> qty equals the computed base*factor, never more.
    r = _size(confidence=1.0)
    assert r.qty == 500


def test_non_finite_inputs_are_safe():
    assert _size(entry_price=float("nan")).reason == "SIZED_ZERO_BAD_INPUTS"
    assert _size(entry_price=float("inf")).reason == "SIZED_ZERO_BAD_INPUTS"
    assert _size(equity=float("nan")).reason == "SIZED_ZERO_BAD_INPUTS"
    # a non-finite signal_stop must not be used as a distance; falls back safely.
    r = _size(signal_stop=float("nan"), cfg=_cfg(trailing_stop_pct=0.0), fixed_qty=2)
    assert r.qty == 2 and r.reason == "NO_STOP_DISTANCE_FALLBACK"


def test_min_confidence_equal_one_does_not_divide_by_zero():
    r = _size(cfg=_cfg(min_confidence=1.0), confidence=1.0)
    assert math.isfinite(r.qty) and r.qty == 500  # factor clamps to ceil (1.0)
