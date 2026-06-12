import pytest
from autotrader.domain import Signal, Position
from autotrader.strategies.threshold import ThresholdStrategy, StrategyParams


def _params(**over):
    base = dict(symbol="US.AAPL", entry_price=100.0, stop_loss_pct=0.05,
                take_profit_pct=0.10, confidence=0.7)
    base.update(over)
    return StrategyParams(**base)


def test_params_require_explicit_stop_and_target():
    with pytest.raises(ValueError):
        StrategyParams(symbol="US.AAPL", entry_price=100.0, stop_loss_pct=0.0,
                       take_profit_pct=0.10, confidence=0.7)
    with pytest.raises(ValueError):
        StrategyParams(symbol="US.AAPL", entry_price=100.0, stop_loss_pct=0.05,
                       take_profit_pct=0.0, confidence=0.7)


def test_buy_signal_when_price_crosses_entry_and_flat():
    s = ThresholdStrategy(_params())
    sig = s.evaluate(price=100.5, position=None)
    assert isinstance(sig, Signal)
    assert sig.direction == "BUY"
    assert sig.confidence == 0.7


def test_no_signal_when_below_entry():
    s = ThresholdStrategy(_params())
    assert s.evaluate(price=99.0, position=None) is None


def test_no_duplicate_buy_when_already_long():
    s = ThresholdStrategy(_params())
    pos = Position("US.AAPL", qty=10, avg_price=100.0)
    assert s.evaluate(price=101.0, position=pos) is None


def test_sell_signal_on_stop_loss():
    s = ThresholdStrategy(_params())
    pos = Position("US.AAPL", qty=10, avg_price=100.0)
    sig = s.evaluate(price=94.9, position=pos)  # -5.1% < -5% stop
    assert sig.direction == "SELL"


def test_sell_signal_on_take_profit():
    s = ThresholdStrategy(_params())
    pos = Position("US.AAPL", qty=10, avg_price=100.0)
    sig = s.evaluate(price=110.5, position=pos)  # +10.5% > +10% target
    assert sig.direction == "SELL"


def test_strategy_is_deterministic_no_internal_state():
    s = ThresholdStrategy(_params())
    a = s.evaluate(price=100.5, position=None)
    b = s.evaluate(price=100.5, position=None)
    assert a == b  # same inputs -> same output, no tick-to-tick state
