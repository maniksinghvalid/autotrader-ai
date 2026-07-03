from autotrader.config import RiskConfig
from autotrader.domain import AccountSnapshot
from autotrader.risk_check import evaluate, RiskAction


def _cfg():
    return RiskConfig(
        trading_env="PAPER", min_confidence=0.6, max_order_notional=2000,
        max_position_qty=100, daily_loss_limit=500, max_gross_exposure=50000,
        allowed_symbols=frozenset({"US.AAPL"}), daily_loss_halt=1000)


def _snap(day_pnl):
    return AccountSnapshot(cash=0.0, total_assets=0.0, day_pnl=day_pnl,
                           stale=False, positions=())


def test_ok_when_no_breach():
    assert evaluate(_snap(-100.0), _cfg()) is RiskAction.OK


def test_gate_on_soft_breach():
    assert evaluate(_snap(-600.0), _cfg()) is RiskAction.GATE


def test_halt_on_hard_breach():
    assert evaluate(_snap(-1200.0), _cfg()) is RiskAction.HALT


def test_boundary_soft_inclusive():
    assert evaluate(_snap(-500.0), _cfg()) is RiskAction.GATE


def test_boundary_hard_inclusive():
    assert evaluate(_snap(-1000.0), _cfg()) is RiskAction.HALT
