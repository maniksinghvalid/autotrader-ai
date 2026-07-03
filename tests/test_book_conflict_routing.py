"""External signals (plain + overlay) are skipped on breakout-claimed symbols;
internal breakout routing is unaffected."""
from autotrader.config import RiskConfig
from autotrader.db import DB
from autotrader.domain import OverlayType, Signal
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


def _engine(tmp_path, broker, cfg):
    strat = ThresholdStrategy(StrategyParams("US.NIO", 1.0, 0.05, 0.10, 0.7))
    db = DB(str(tmp_path / "t.db"))
    eng = TradeEngine(broker, strat, cfg, order_qty=1,
                      audit_path=str(tmp_path / "audit.jsonl"), db=db,
                      entry_gate=EntryGate(enabled=True))
    return eng, db


def test_external_buy_skipped_when_symbol_breakout_claimed(tmp_path):
    broker = SimBroker({"US.NIO": 5.0})
    eng, db = _engine(tmp_path, broker, _cfg())
    db.claim_symbol("US.NIO", "BREAKOUT", "sess-x")
    res = eng.submit_external_signal(Signal("US.NIO", "BUY", 0.9, "ext"))
    assert res.action == "BOOK_CONFLICT"
    db.close()


def test_external_overlay_skipped_when_symbol_breakout_claimed(tmp_path):
    broker = SimBroker({"US.NIO": 5.0})
    eng, db = _engine(tmp_path, broker, _cfg())
    db.claim_symbol("US.NIO", "BREAKOUT", "sess-x")
    res = eng.submit_external_signal(
        Signal("US.NIO", "SELL", 0.9, "ext", overlay=OverlayType.COVERED_CALL))
    assert res.action == "BOOK_CONFLICT"
    db.close()


def test_external_sell_also_skipped_when_breakout_claimed(tmp_path):
    broker = SimBroker({"US.NIO": 5.0})
    eng, db = _engine(tmp_path, broker, _cfg())
    db.claim_symbol("US.NIO", "BREAKOUT", "sess-x")
    res = eng.submit_external_signal(Signal("US.NIO", "SELL", 0.9, "ext"))
    assert res.action == "BOOK_CONFLICT"
    db.close()


def test_external_unclaimed_symbol_routes_normally(tmp_path):
    broker = SimBroker({"US.NIO": 5.0})
    eng, db = _engine(tmp_path, broker, _cfg())
    res = eng.submit_external_signal(Signal("US.NIO", "BUY", 0.9, "ext"))
    assert res.action == "ORDER_PLACED"  # no claim -> AI book routes as today
    db.close()
