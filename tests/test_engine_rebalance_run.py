from datetime import datetime, timedelta, timezone

from autotrader.config import RiskConfig
from autotrader.db import DB
from autotrader.lifecycle import EntryGate
from autotrader.main import TradeEngine
from autotrader.sim_broker import SimBroker
from autotrader.strategies.threshold import StrategyParams, ThresholdStrategy

NY_NOW = datetime(2026, 6, 16, 12, 30, tzinfo=timezone.utc)


def _cfg(**kw):
    base = dict(trading_env="PAPER", min_confidence=0.6, max_order_notional=1e9,
                max_position_qty=10_000, daily_loss_limit=500,
                max_gross_exposure=1e9, allowed_symbols=frozenset({"US.AAPL"}),
                trailing_stop_pct=5.0, rebalance_enabled=True,
                rebalance_band_pct=5.0, rebalance_min_notional=200.0,
                rebalance_cash_buffer_pct=0.0, target_staleness_hours=24.0)
    base.update(kw)
    return RiskConfig(**base)


def _engine(tmp_path, broker, cfg, gate):
    strat = ThresholdStrategy(StrategyParams("US.AAPL", 1.0, 0.05, 0.10, 0.7))
    db = DB(str(tmp_path / "t.db"))
    eng = TradeEngine(broker, strat, cfg, order_qty=1,
                      audit_path=str(tmp_path / "audit.jsonl"), db=db,
                      entry_gate=gate)
    return eng, db


def test_rebalance_noop_when_disabled(tmp_path):
    broker = SimBroker({"US.AAPL": 100.0})
    eng, db = _engine(tmp_path, broker, _cfg(rebalance_enabled=False),
                      EntryGate(enabled=True))
    db.upsert_target_weights("2026-06-16", [("US.AAPL", 100.0)])
    summary = eng.rebalance(NY_NOW)
    assert summary == "REBALANCE_DISABLED"
    db.close()


def test_rebalance_noop_when_no_targets(tmp_path):
    broker = SimBroker({"US.AAPL": 100.0})
    eng, db = _engine(tmp_path, broker, _cfg(), EntryGate(enabled=True))
    assert eng.rebalance(NY_NOW) == "NO_TARGETS"
    db.close()


def test_rebalance_skips_stale_snapshot(tmp_path):
    broker = SimBroker({"US.AAPL": 100.0})
    eng, db = _engine(tmp_path, broker, _cfg(target_staleness_hours=1.0),
                      EntryGate(enabled=True))
    db.upsert_target_weights("2026-06-16", [("US.AAPL", 100.0)])
    # upsert stamps ingested_at with the real wall clock; pin it to the synthetic
    # clock so staleness is measured against NY_NOW, not today's actual date
    # (otherwise the fixed `later` below drifts relative to a moving ingest time).
    db._conn.execute("UPDATE target_weights SET ingested_at=?", (NY_NOW.isoformat(),))
    db._conn.commit()
    later = NY_NOW + timedelta(hours=48)   # ingested 48h before `later`; staleness=1h
    assert eng.rebalance(later) == "STALE_TARGETS"
    db.close()


def test_rebalance_tops_up_and_attaches_stop(tmp_path):
    broker = SimBroker({"US.AAPL": 100.0}, cash=10000.0)
    gate = EntryGate(enabled=True)
    eng, db = _engine(tmp_path, broker, _cfg(), gate)
    db.upsert_target_weights("2026-06-16", [("US.AAPL", 100.0)])
    summary = eng.rebalance(NY_NOW)
    assert summary == "REBALANCED"
    assert broker.get_account().position_qty("US.AAPL") > 0
    assert db.get_open_trailing_stop("US.AAPL") is not None
    db.close()


def test_rebalance_halted_is_noop(tmp_path):
    broker = SimBroker({"US.AAPL": 100.0}, cash=10000.0)
    gate = EntryGate(enabled=True)
    gate.halt()
    eng, db = _engine(tmp_path, broker, _cfg(), gate)
    db.upsert_target_weights("2026-06-16", [("US.AAPL", 100.0)])
    assert eng.rebalance(NY_NOW) == "HALTED"
    assert broker.get_account().position_qty("US.AAPL") == 0
    db.close()
