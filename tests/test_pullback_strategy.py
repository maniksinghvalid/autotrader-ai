"""Pullback (mean-reversion) strategy: dip-in-uptrend entries, fail-safe on
missing references, shared exit helper. Mirrors test conventions of the
breakout strategy tests."""
from __future__ import annotations

import pytest

from autotrader.domain import Position
from autotrader.strategies.breakout import BreakoutParams, BreakoutStrategy
from autotrader.strategies.pullback import PullbackParams, PullbackStrategy


def _strategy(**over):
    base = dict(symbol="US.AAPL", stop_loss_pct=0.03, take_profit_pct=0.03,
               confidence=0.7)
    base.update(over)
    return PullbackStrategy(PullbackParams(**base))


def test_dip_in_uptrend_buys():
    s = _strategy()
    sig = s.evaluate(95.0, None, ref_low=95.5, sma=90.0)
    assert sig is not None and sig.direction == "BUY"


def test_dip_at_exact_ref_low_buys():
    """price <= ref_low is inclusive — touching the reference low counts."""
    s = _strategy()
    sig = s.evaluate(95.0, None, ref_low=95.0, sma=90.0)
    assert sig is not None and sig.direction == "BUY"


def test_dip_in_downtrend_never_buys():
    """The regime gate: price below the SMA means no entry no matter how deep
    the dip."""
    s = _strategy()
    assert s.evaluate(80.0, None, ref_low=95.0, sma=90.0) is None


def test_missing_ref_low_no_entry():
    s = _strategy()
    assert s.evaluate(95.0, None, ref_low=None, sma=90.0) is None


def test_missing_sma_no_entry():
    s = _strategy()
    assert s.evaluate(95.0, None, ref_low=95.5, sma=None) is None


def test_no_dip_no_entry():
    s = _strategy()
    assert s.evaluate(99.0, None, ref_low=95.0, sma=90.0) is None


def test_held_routes_to_shared_exit_stop_before_target():
    s = _strategy(stop_loss_pct=0.05, take_profit_pct=0.10)
    pos = Position("US.AAPL", 10, 100.0)
    sig = s.evaluate(94.0, pos, ref_low=None, sma=None)   # -6% <= -5%
    assert sig is not None and sig.direction == "SELL"
    assert "stop-loss" in sig.rationale


def test_held_take_profit():
    s = _strategy(stop_loss_pct=0.05, take_profit_pct=0.10)
    pos = Position("US.AAPL", 10, 100.0)
    sig = s.evaluate(111.0, pos, ref_low=None, sma=None)
    assert sig is not None and "take-profit" in sig.rationale


@pytest.mark.parametrize("field,value", [
    ("stop_loss_pct", 0.0), ("stop_loss_pct", -0.05),
    ("take_profit_pct", 0.0), ("take_profit_pct", -0.1),
])
def test_params_reject_missing_stop_or_target(field, value):
    with pytest.raises(ValueError):
        _strategy(**{field: value})


def test_breakout_sma_kwarg_omitted_is_backward_compatible():
    """Existing 3-arg call shape behaves exactly as before the kwarg existed."""
    s = BreakoutStrategy(BreakoutParams("US.AAPL", 0.05, 0.10, 0.7))
    sig = s.evaluate(101.0, None, 100.0)
    assert sig is not None and sig.direction == "BUY"


def test_breakout_sma_above_price_blocks_entry():
    s = BreakoutStrategy(BreakoutParams("US.AAPL", 0.05, 0.10, 0.7))
    assert s.evaluate(101.0, None, ref_high=100.0, sma=150.0) is None


def test_breakout_sma_below_price_allows_entry():
    s = BreakoutStrategy(BreakoutParams("US.AAPL", 0.05, 0.10, 0.7))
    sig = s.evaluate(101.0, None, ref_high=100.0, sma=90.0)
    assert sig is not None and sig.direction == "BUY"
