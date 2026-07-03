from datetime import date, datetime

from autotrader.db import DB
from autotrader.lifecycle import EntryGate, restore_session_halt


def test_restore_halts_gate_when_todays_halt_persisted(tmp_path):
    db = DB(str(tmp_path / "t.db"))
    db.set_state("halt:2026-07-06", "daily loss halt: pnl=-2000")
    gate = EntryGate(enabled=False)
    assert restore_session_halt(gate, db, date(2026, 7, 6)) is True
    assert gate.halted


def test_no_restore_on_other_day(tmp_path):
    db = DB(str(tmp_path / "t.db"))
    db.set_state("halt:2026-07-05", "yesterday")
    gate = EntryGate(enabled=False)
    assert restore_session_halt(gate, db, date(2026, 7, 6)) is False
    assert not gate.halted


def test_apply_risk_check_persists_halt(tmp_path):
    from autotrader.config import RiskConfig
    from autotrader.main import TradeEngine
    from autotrader.sim_broker import SimBroker
    from autotrader.strategies.breakout import BreakoutParams, BreakoutStrategy

    class _LossBroker(SimBroker):
        def get_account(self):
            snap = super().get_account()
            object.__setattr__(snap, "day_pnl", -5000.0)   # frozen dataclass poke
            return snap

    db = DB(str(tmp_path / "t.db"))
    cfg = RiskConfig(trading_env="PAPER", min_confidence=0.5, max_order_notional=1e6,
                     max_position_qty=1000, daily_loss_limit=500, max_gross_exposure=1e6,
                     allowed_symbols=frozenset(), daily_loss_halt=1000.0)
    strat = BreakoutStrategy(BreakoutParams(symbol="US.TEST", stop_loss_pct=0.05,
                                            take_profit_pct=0.10, confidence=0.9))
    gate = EntryGate(enabled=True)
    eng = TradeEngine(_LossBroker({"US.TEST": 100.0}), strat, cfg, order_qty=1,
                      audit_path=str(tmp_path / "a.jsonl"), db=db, entry_gate=gate)
    assert eng.apply_risk_check(datetime(2026, 7, 6, 13, 30)) == "HALT"
    assert db.get_state("halt:2026-07-06") is not None
