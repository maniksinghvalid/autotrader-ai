"""reconcile_claims releases claims whose position has gone flat."""
from autotrader.config import RiskConfig
from autotrader.db import DB
from autotrader.domain import Position
from autotrader.lifecycle import EntryGate
from autotrader.main import TradeEngine
from autotrader.sim_broker import SimBroker
from autotrader.strategies.threshold import StrategyParams, ThresholdStrategy


def _cfg():
    return RiskConfig(trading_env="PAPER", min_confidence=0.6,
                      max_order_notional=1e9, max_position_qty=10_000,
                      daily_loss_limit=500, max_gross_exposure=1e9,
                      allowed_symbols=frozenset({"US.NIO"}),
                      trailing_stop_pct=0.0, daily_loss_halt=1000)


def _engine(tmp_path):
    broker = SimBroker({"US.NIO": 5.0})
    strat = ThresholdStrategy(StrategyParams("US.NIO", 1.0, 0.05, 0.10, 0.7))
    db = DB(str(tmp_path / "t.db"))
    eng = TradeEngine(broker, strat, _cfg(), order_qty=1,
                      audit_path=str(tmp_path / "audit.jsonl"), db=db,
                      entry_gate=EntryGate(enabled=True))
    return eng, db


def test_reconcile_releases_claim_for_flat_symbol(tmp_path):
    eng, db = _engine(tmp_path)
    db.claim_symbol("US.NIO", "BREAKOUT", "sess-x")
    db.replace_positions([])  # ground truth: nothing held
    assert eng.reconcile_claims() == 1
    assert db.get_claims() == {}
    db.close()


def test_reconcile_keeps_claim_for_held_symbol(tmp_path):
    eng, db = _engine(tmp_path)
    db.claim_symbol("US.NIO", "BREAKOUT", "sess-x")
    db.replace_positions([Position("US.NIO", 10, 5.0)])
    assert eng.reconcile_claims() == 0
    assert db.get_claims() == {"US.NIO": "BREAKOUT"}
    db.close()


def test_reconcile_noop_without_db(tmp_path):
    broker = SimBroker({"US.NIO": 5.0})
    strat = ThresholdStrategy(StrategyParams("US.NIO", 1.0, 0.05, 0.10, 0.7))
    eng = TradeEngine(broker, strat, _cfg(), order_qty=1,
                      audit_path=str(tmp_path / "audit.jsonl"), db=None,
                      entry_gate=EntryGate(enabled=True))
    assert eng.reconcile_claims() == 0


def test_runner_risk_sweep_reconciles_claims(tmp_path):
    """A claim on a now-flat symbol is released when RISK_SWEEP syncs."""
    from datetime import datetime, timezone

    from autotrader.runner import SessionRunner
    from autotrader.scheduler import RISK_SWEEP

    broker = SimBroker({"US.NIO": 5.0})
    strat = ThresholdStrategy(StrategyParams("US.NIO", 1.0, 0.05, 0.10, 0.7))
    db = DB(str(tmp_path / "t.db"))
    eng = TradeEngine(broker, strat, _cfg(), order_qty=1,
                      audit_path=str(tmp_path / "audit.jsonl"), db=db,
                      entry_gate=EntryGate(enabled=True))
    db.claim_symbol("US.NIO", "BREAKOUT", "sess-x")
    # broker holds nothing -> ground_truth_sync writes empty positions

    gate = EntryGate(enabled=True)
    runner = SessionRunner(engine=eng, broker=broker, db=db, gate=gate,
                           scheduler=None, watchdog=None, clock=None,
                           sleep=lambda s: None, loop_interval=5.0)
    runner._run_job(RISK_SWEEP, datetime(2026, 7, 3, 19, 30, tzinfo=timezone.utc))
    assert db.get_claims() == {}
    db.close()
