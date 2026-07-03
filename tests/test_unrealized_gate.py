from autotrader.config import RiskConfig
from autotrader.domain import AccountSnapshot
from autotrader.risk_check import evaluate, RiskAction


def _cfg(gate=300.0):
    return RiskConfig(trading_env="PAPER", min_confidence=0.5,
                      max_order_notional=1e6, max_position_qty=1000,
                      daily_loss_limit=500, max_gross_exposure=1e6,
                      allowed_symbols=frozenset(), unrealized_loss_gate=gate)


def _snap(unreal):
    return AccountSnapshot(cash=1000.0, total_assets=1000.0, day_pnl=0.0,
                           stale=False, unrealized_pnl=unreal)


def test_breach_gates():
    assert evaluate(_snap(-301.0), _cfg()) is RiskAction.GATE


def test_breach_never_halts():
    assert evaluate(_snap(-99999.0), _cfg()) is not RiskAction.HALT


def test_zero_config_disables():
    assert evaluate(_snap(-99999.0), _cfg(gate=0.0)) is RiskAction.OK


def test_realized_halt_still_wins():
    snap = AccountSnapshot(cash=0, total_assets=0, day_pnl=-2000.0, stale=False,
                           unrealized_pnl=-301.0)
    assert evaluate(snap, _cfg()) is RiskAction.HALT
