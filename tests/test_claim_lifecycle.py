"""Breakout BUY claims before submit; a filled breakout SELL releases inline."""
from autotrader.config import RiskConfig
from autotrader.db import DB
from autotrader.domain import Position, Signal
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


def test_breakout_buy_writes_claim(tmp_path):
    broker = SimBroker({"US.NIO": 5.0})
    eng, db = _engine(tmp_path, broker, _cfg())
    res = eng._route_signal(Signal("US.NIO", "BUY", 0.9, "breakout"),
                            broker.get_account(), 5.0, origin="BREAKOUT")
    assert res.action == "ORDER_PLACED"
    assert db.get_claims() == {"US.NIO": "BREAKOUT"}
    db.close()


def test_claim_written_before_submit_when_broker_raises(tmp_path):
    class RaisingBroker(SimBroker):
        def place_order(self, req):
            raise RuntimeError("broker down")

    broker = RaisingBroker({"US.NIO": 5.0})
    eng, db = _engine(tmp_path, broker, _cfg())
    try:
        eng._route_signal(Signal("US.NIO", "BUY", 0.9, "breakout"),
                          broker.get_account(), 5.0, origin="BREAKOUT")
    except RuntimeError:
        pass
    # Fail-closed ordering: the claim exists even though the order never acked.
    assert db.get_claims() == {"US.NIO": "BREAKOUT"}
    db.close()


def test_filled_breakout_sell_releases_claim(tmp_path):
    broker = SimBroker({"US.NIO": 5.0})
    broker._positions["US.NIO"] = Position("US.NIO", 1, 5.0)  # seed pattern from test_halt_flatten_order.py
    eng, db = _engine(tmp_path, broker, _cfg())
    db.claim_symbol("US.NIO", "BREAKOUT", "sess-x")
    res = eng._route_signal(Signal("US.NIO", "SELL", 0.9, "breakout"),
                            broker.get_account(), 5.0, origin="BREAKOUT")
    assert res.action == "ORDER_PLACED"
    assert db.get_claims() == {}   # SimBroker auto_fill -> FILLED ack -> released inline
    db.close()
