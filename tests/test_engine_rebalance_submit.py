from autotrader.config import RiskConfig
from autotrader.db import DB
from autotrader.lifecycle import EntryGate
from autotrader.main import TradeEngine
from autotrader.rebalance import RebalanceTrade
from autotrader.sim_broker import SimBroker
from autotrader.strategies.threshold import StrategyParams, ThresholdStrategy


def _cfg(**kw):
    base = dict(trading_env="PAPER", min_confidence=0.6, max_order_notional=1e9,
                max_position_qty=10_000, daily_loss_limit=500,
                max_gross_exposure=1e9, allowed_symbols=frozenset({"US.AAPL"}),
                trailing_stop_pct=5.0)
    base.update(kw)
    return RiskConfig(**base)


def _engine(tmp_path, broker, cfg, gate):
    strat = ThresholdStrategy(StrategyParams("US.AAPL", 1.0, 0.05, 0.10, 0.7))
    db = DB(str(tmp_path / "t.db"))
    eng = TradeEngine(broker, strat, cfg, order_qty=1,
                      audit_path=str(tmp_path / "audit.jsonl"), db=db,
                      entry_gate=gate)
    return eng, db


def test_partial_sell_routes_through_risk_and_records(tmp_path):
    broker = SimBroker({"US.AAPL": 100.0})
    gate = EntryGate(enabled=True)
    eng, db = _engine(tmp_path, broker, _cfg(), gate)
    from autotrader.domain import OrderRequest
    broker.place_order(OrderRequest("US.AAPL", "BUY", 80, "MARKET", None, "seed"))
    trade = RebalanceTrade("US.AAPL", "SELL", 35, "TRIM", 45)
    res = eng.submit_rebalance_order(trade, ref_price=100.0, round_id="rbal-x")
    assert res.action == "ORDER_PLACED"
    row = db._conn.execute(
        "SELECT side, qty, order_type FROM trades WHERE qty=35").fetchone()
    assert row == ("SELL", 35, "MARKET")
    db.close()


def test_rebalance_order_records_driver_for_eod_attribution(tmp_path):
    # A placed rebalance order records a `drivers` row so the EOD report can
    # explain a trade that has no driving Signal (rebalance trades never create one).
    broker = SimBroker({"US.AAPL": 100.0})
    gate = EntryGate(enabled=True)
    eng, db = _engine(tmp_path, broker, _cfg(), gate)
    from autotrader.domain import OrderRequest
    broker.place_order(OrderRequest("US.AAPL", "BUY", 80, "MARKET", None, "seed"))
    trade = RebalanceTrade("US.AAPL", "SELL", 35, "TRIM", 45)
    res = eng.submit_rebalance_order(trade, ref_price=100.0, round_id="rbal-2026-06-17")
    assert res.action == "ORDER_PLACED"
    row = db._conn.execute(
        "SELECT symbol, side, kind, detail FROM drivers WHERE symbol='US.AAPL'").fetchone()
    assert row is not None
    assert row[0] == "US.AAPL" and row[1] == "SELL" and row[2] == "rebalance"
    assert "rbal-2026-06-17" in row[3] and "trim" in row[3].lower()
    db.close()


def test_topup_blocked_when_gate_closed(tmp_path):
    broker = SimBroker({"US.AAPL": 100.0})
    gate = EntryGate(enabled=False)
    eng, db = _engine(tmp_path, broker, _cfg(), gate)
    trade = RebalanceTrade("US.AAPL", "BUY", 10, "TOPUP", 10)
    res = eng.submit_rebalance_order(trade, ref_price=100.0, round_id="rbal-x")
    assert res.action == "ENTRY_CLOSED"
    db.close()


def test_submit_blocked_when_halted(tmp_path):
    broker = SimBroker({"US.AAPL": 100.0})
    gate = EntryGate(enabled=True)
    gate.halt()
    eng, db = _engine(tmp_path, broker, _cfg(), gate)
    trade = RebalanceTrade("US.AAPL", "SELL", 5, "TRIM", 0)
    res = eng.submit_rebalance_order(trade, ref_price=100.0, round_id="rbal-x")
    assert res.action == "HALTED"
    db.close()


def test_consolidate_stop_replaces_old_stop(tmp_path):
    broker = SimBroker({"US.AAPL": 100.0})
    gate = EntryGate(enabled=True)
    eng, db = _engine(tmp_path, broker, _cfg(), gate)
    from autotrader.domain import OrderRequest
    broker.place_order(OrderRequest("US.AAPL", "BUY", 45, "MARKET", None, "seed"))
    eng.consolidate_stop("US.AAPL", new_total_qty=10, ref_price=100.0, round_id="r1")
    first = db.get_open_trailing_stop("US.AAPL")
    assert first is not None
    eng.consolidate_stop("US.AAPL", new_total_qty=45, ref_price=100.0, round_id="r2")
    second = db.get_open_trailing_stop("US.AAPL")
    assert second is not None and second != first
    row = db._conn.execute(
        "SELECT qty FROM trades WHERE broker_order_id=?", (second,)).fetchone()
    assert row[0] == 45
    db.close()


def test_consolidate_to_zero_cancels_without_replacement(tmp_path):
    broker = SimBroker({"US.AAPL": 100.0})
    gate = EntryGate(enabled=True)
    eng, db = _engine(tmp_path, broker, _cfg(), gate)
    from autotrader.domain import OrderRequest
    broker.place_order(OrderRequest("US.AAPL", "BUY", 10, "MARKET", None, "seed"))
    eng.consolidate_stop("US.AAPL", new_total_qty=10, ref_price=100.0, round_id="r1")
    assert db.get_open_trailing_stop("US.AAPL") is not None
    eng.consolidate_stop("US.AAPL", new_total_qty=0, ref_price=100.0, round_id="r2")
    assert db.get_open_trailing_stop("US.AAPL") is None
    db.close()
