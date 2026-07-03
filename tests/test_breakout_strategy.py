from autotrader.domain import Position
from autotrader.strategies.breakout import BreakoutParams, BreakoutStrategy


def _s():
    return BreakoutStrategy(BreakoutParams("US.AAPL", 0.05, 0.10, 0.7))


def test_breakout_above_ref_high_buys():
    sig = _s().evaluate(price=131.0, position=None, ref_high=130.5)
    assert sig is not None and sig.direction == "BUY" and "breakout" in sig.rationale


def test_equal_to_ref_high_does_not_buy():
    assert _s().evaluate(price=130.5, position=None, ref_high=130.5) is None


def test_below_ref_high_does_not_buy():
    assert _s().evaluate(price=129.0, position=None, ref_high=130.5) is None


def test_none_ref_high_never_buys():
    assert _s().evaluate(price=999.0, position=None, ref_high=None) is None


def test_holding_stop_loss_sells():
    sig = _s().evaluate(price=95.0, position=Position("US.AAPL", 10, 100.0), ref_high=None)
    assert sig is not None and sig.direction == "SELL" and "stop-loss" in sig.rationale


def test_holding_take_profit_sells():
    sig = _s().evaluate(price=110.0, position=Position("US.AAPL", 10, 100.0), ref_high=1.0)
    assert sig is not None and sig.direction == "SELL" and "take-profit" in sig.rationale


def test_holding_within_band_holds():
    assert _s().evaluate(price=101.0, position=Position("US.AAPL", 10, 100.0), ref_high=1.0) is None


def test_params_reject_nonpositive_stop():
    import pytest
    with pytest.raises(ValueError):
        BreakoutParams("US.AAPL", 0.0, 0.10, 0.7)
