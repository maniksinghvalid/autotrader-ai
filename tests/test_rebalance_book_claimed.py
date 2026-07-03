"""Rebalance never trims or tops-up a breakout-claimed symbol."""
from datetime import datetime, timezone

from autotrader.config import RiskConfig
from autotrader.db import DB
from autotrader.domain import Position
from autotrader.lifecycle import EntryGate
from autotrader.main import TradeEngine
from autotrader.sim_broker import SimBroker
from autotrader.strategies.threshold import StrategyParams, ThresholdStrategy

NOW = datetime(2026, 7, 3, 16, 30, tzinfo=timezone.utc)


def _cfg(**kw):
    base = dict(trading_env="PAPER", min_confidence=0.6, max_order_notional=1e9,
                max_position_qty=10_000, daily_loss_limit=500,
                max_gross_exposure=1e9,
                allowed_symbols=frozenset({"US.NIO", "US.AAPL"}),
                trailing_stop_pct=0.0, daily_loss_halt=1000,
                rebalance_enabled=True, target_staleness_hours=48)
    base.update(kw)
    return RiskConfig(**base)


def _engine(tmp_path, broker, cfg):
    strat = ThresholdStrategy(StrategyParams("US.NIO", 1e9, 0.05, 0.10, 0.7))
    db = DB(str(tmp_path / "t.db"))
    eng = TradeEngine(broker, strat, cfg, order_qty=1,
                      audit_path=str(tmp_path / "audit.jsonl"), db=db,
                      entry_gate=EntryGate(enabled=True))
    return eng, db


def test_claimed_symbol_not_traded_by_rebalance(tmp_path):
    # NIO is heavily overweight (qty=1000 vs. a small target score) so, absent
    # the claim filter, compute_plan would trim it — the scenario this test
    # guards against. US.AAPL gets the rest of the investable weight.
    broker = SimBroker({"US.NIO": 5.0, "US.AAPL": 100.0})
    broker._positions["US.NIO"] = Position("US.NIO", 1000, 5.0)
    eng, db = _engine(tmp_path, broker, _cfg())
    # upsert_target_weights(as_of_date, [(symbol, score), ...]); it stamps
    # ingested_at = _now() (UTC). Force it to NOW so `now - ingested_at` is fresh
    # (pattern from tests/test_engine_rebalance_run.py:58).
    db.upsert_target_weights("2026-07-03", [("US.NIO", 0.01), ("US.AAPL", 0.99)])
    db._conn.execute("UPDATE target_weights SET ingested_at=?", (NOW.isoformat(),))
    db._conn.commit()
    db.claim_symbol("US.NIO", "BREAKOUT", "sess-x")

    eng.rebalance(NOW)

    # No trade row for the claimed symbol (breakout owns its lifecycle).
    rows = db._conn.execute(
        "SELECT symbol FROM trades WHERE symbol=?", ("US.NIO",)).fetchall()
    assert rows == []
    db.close()
