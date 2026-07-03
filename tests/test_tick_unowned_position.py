"""The breakout tick declines to manage a held-but-unclaimed (AI-book) position."""
from autotrader.config import RiskConfig
from autotrader.db import DB
from autotrader.domain import Position
from autotrader.lifecycle import EntryGate
from autotrader.main import TradeEngine
from autotrader.sim_broker import SimBroker
from autotrader.strategies.threshold import StrategyParams, ThresholdStrategy


def _cfg(**kw):
    base = dict(trading_env="PAPER", min_confidence=0.6, max_order_notional=1e9,
                max_position_qty=10_000, daily_loss_limit=500,
                max_gross_exposure=1e9, allowed_symbols=frozenset({"US.NIO"}),
                trailing_stop_pct=0.0, daily_loss_halt=1000)
    base.update(kw)
    return RiskConfig(**base)


def _engine(tmp_path, broker):
    # entry_price 1.0 so a held position at price 0.90 would normally hit the
    # -5% stop (SELL) — proving it's the claim check, not "no signal", that skips.
    strat = ThresholdStrategy(StrategyParams("US.NIO", 1.0, 0.05, 0.10, 0.7))
    db = DB(str(tmp_path / "t.db"))
    eng = TradeEngine(broker, strat, _cfg(), order_qty=1,
                      audit_path=str(tmp_path / "audit.jsonl"), db=db,
                      entry_gate=EntryGate(enabled=True))
    return eng, db


def test_tick_skips_held_unclaimed_position(tmp_path):
    broker = SimBroker({"US.NIO": 0.90})
    broker._positions["US.NIO"] = Position("US.NIO", 10, 1.0)
    eng, db = _engine(tmp_path, broker)
    # No claim -> AI book owns it -> breakout tick must not manage the exit.
    assert eng.tick().action == "POSITION_NOT_OWNED"
    db.close()


def test_tick_manages_held_claimed_position(tmp_path):
    broker = SimBroker({"US.NIO": 0.90})
    broker._positions["US.NIO"] = Position("US.NIO", 10, 1.0)
    eng, db = _engine(tmp_path, broker)
    db.claim_symbol("US.NIO", "BREAKOUT", "sess-x")
    # Claimed -> breakout owns it -> the -10% move triggers a stop-loss SELL.
    assert eng.tick().action == "ORDER_PLACED"
    db.close()
