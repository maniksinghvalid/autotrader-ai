from autotrader.domain import Position
from autotrader.strategies.exits import manage_long_exit


def _pos(qty, avg):
    return Position("US.AAPL", qty, avg)


def test_stop_loss_triggers_sell():
    s = manage_long_exit("US.AAPL", 95.0, _pos(10, 100.0), 0.05, 0.10, 0.7)
    assert s is not None and s.direction == "SELL" and "stop-loss" in s.rationale


def test_take_profit_triggers_sell():
    s = manage_long_exit("US.AAPL", 110.0, _pos(10, 100.0), 0.05, 0.10, 0.7)
    assert s is not None and s.direction == "SELL" and "take-profit" in s.rationale


def test_within_band_returns_none():
    assert manage_long_exit("US.AAPL", 102.0, _pos(10, 100.0), 0.05, 0.10, 0.7) is None


def test_flat_returns_none():
    assert manage_long_exit("US.AAPL", 102.0, None, 0.05, 0.10, 0.7) is None
