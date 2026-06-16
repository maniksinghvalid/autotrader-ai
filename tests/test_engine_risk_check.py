from datetime import datetime, timezone

from autotrader.config import RiskConfig
from autotrader.db import DB
from autotrader.domain import AccountSnapshot, Position
from autotrader.lifecycle import EntryGate
from autotrader.main import TradeEngine
from autotrader.sim_broker import SimBroker
from autotrader.strategies.threshold import StrategyParams, ThresholdStrategy

NOW = datetime(2026, 6, 16, 13, 30, tzinfo=timezone.utc)


class PnlBroker(SimBroker):
    """SimBroker whose account reports a fixed day_pnl, for risk-check tests."""
    def __init__(self, quotes, day_pnl, **kw):
        super().__init__(quotes, **kw)
        self._day_pnl = day_pnl

    def get_account(self):
        base = super().get_account()
        return AccountSnapshot(cash=base.cash, total_assets=base.total_assets,
                               day_pnl=self._day_pnl, stale=False,
                               positions=base.positions)


def _cfg(**kw):
    base = dict(trading_env="PAPER", min_confidence=0.6, max_order_notional=1e9,
                max_position_qty=10_000, daily_loss_limit=500,
                max_gross_exposure=1e9, allowed_symbols=frozenset({"US.AAPL"}),
                trailing_stop_pct=5.0, daily_loss_halt=1000)
    base.update(kw)
    return RiskConfig(**base)


def _engine(tmp_path, broker, cfg, gate):
    strat = ThresholdStrategy(StrategyParams("US.AAPL", 1.0, 0.05, 0.10, 0.7))
    db = DB(str(tmp_path / "t.db"))
    eng = TradeEngine(broker, strat, cfg, order_qty=1,
                      audit_path=str(tmp_path / "audit.jsonl"), db=db,
                      entry_gate=gate)
    return eng, db


def test_ok_keeps_gate_open(tmp_path):
    broker = PnlBroker({"US.AAPL": 100.0}, day_pnl=-100.0)
    gate = EntryGate(enabled=True)
    eng, db = _engine(tmp_path, broker, _cfg(), gate)
    assert eng.apply_risk_check(NOW) == "OK"
    assert gate.entries_enabled is True
    db.close()


def test_soft_breach_closes_gate_keeps_positions(tmp_path):
    broker = PnlBroker({"US.AAPL": 100.0}, day_pnl=-600.0)
    from autotrader.domain import OrderRequest
    broker.place_order(OrderRequest("US.AAPL", "BUY", 10, "MARKET", None, "seed"))
    gate = EntryGate(enabled=True)
    eng, db = _engine(tmp_path, broker, _cfg(), gate)
    assert eng.apply_risk_check(NOW) == "GATE"
    assert gate.entries_enabled is False
    assert gate.halted is False
    assert broker.get_account().position_qty("US.AAPL") == 10
    db.close()


def test_hard_breach_flattens_and_halts(tmp_path):
    broker = PnlBroker({"US.AAPL": 100.0}, day_pnl=-1200.0)
    from autotrader.domain import OrderRequest
    broker.place_order(OrderRequest("US.AAPL", "BUY", 10, "MARKET", None, "seed"))
    gate = EntryGate(enabled=True)
    eng, db = _engine(tmp_path, broker, _cfg(), gate)
    assert eng.apply_risk_check(NOW) == "HALT"
    assert gate.halted is True
    assert broker.get_account().position_qty("US.AAPL") == 0
    halt = db._conn.execute("SELECT reason FROM halts LIMIT 1").fetchone()
    assert halt is not None
    db.close()
