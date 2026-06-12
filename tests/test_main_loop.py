from autotrader.config import RiskConfig
from autotrader.sim_broker import SimBroker
from autotrader.strategies.threshold import ThresholdStrategy, StrategyParams
from autotrader.main import TradeEngine


def _cfg(**over):
    base = dict(trading_env="PAPER", min_confidence=0.6, max_order_notional=20000,
                max_position_qty=100, daily_loss_limit=500, max_gross_exposure=100000,
                allowed_symbols=frozenset({"US.AAPL"}))
    base.update(over)
    return RiskConfig(**base)


def _engine(broker, cfg=None, qty=10, audit_path="x.jsonl", tmp_path=None):
    path = str(tmp_path / "audit.jsonl") if tmp_path else audit_path
    strat = ThresholdStrategy(StrategyParams(symbol="US.AAPL", entry_price=100.0,
                                             stop_loss_pct=0.05, take_profit_pct=0.10,
                                             confidence=0.7))
    return TradeEngine(broker=broker, strategy=strat, cfg=cfg or _cfg(),
                       order_qty=qty, audit_path=path)


def test_tick_places_buy_when_threshold_crosses(tmp_path):
    b = SimBroker(quotes={"US.AAPL": 101.0}, cash=100000.0)
    eng = _engine(b, tmp_path=tmp_path)
    result = eng.tick()
    assert result.action == "ORDER_PLACED"
    assert b.get_account().position_qty("US.AAPL") == 10


def test_tick_drops_low_confidence_signal(tmp_path):
    b = SimBroker(quotes={"US.AAPL": 101.0}, cash=100000.0)
    eng = _engine(b, cfg=_cfg(min_confidence=0.9), tmp_path=tmp_path)
    result = eng.tick()
    assert result.action == "DROPPED_LOW_CONFIDENCE"
    assert b.get_account().position_qty("US.AAPL") == 0


def test_tick_returns_no_quote_when_symbol_unpriced(tmp_path):
    # Broker has no quote for the strategy's symbol -> NO_QUOTE, no order.
    b = SimBroker(quotes={"US.MSFT": 100.0}, cash=100000.0)
    eng = _engine(b, tmp_path=tmp_path)
    result = eng.tick()
    assert result.action == "NO_QUOTE"
    assert b.get_account().position_qty("US.AAPL") == 0


def test_tick_respects_risk_core_rejection(tmp_path):
    b = SimBroker(quotes={"US.AAPL": 101.0}, cash=100000.0)
    eng = _engine(b, cfg=_cfg(allowed_symbols=frozenset()), tmp_path=tmp_path)
    result = eng.tick()
    assert result.action == "REJECTED_BY_RISK"
    assert b.get_account().position_qty("US.AAPL") == 0


def test_tick_no_signal_when_below_entry(tmp_path):
    b = SimBroker(quotes={"US.AAPL": 99.0}, cash=100000.0)
    eng = _engine(b, tmp_path=tmp_path)
    assert eng.tick().action == "NO_SIGNAL"


def test_shutdown_cancels_all_open_orders(tmp_path):
    b = SimBroker(quotes={"US.AAPL": 101.0}, cash=100000.0, auto_fill=False)
    eng = _engine(b, tmp_path=tmp_path)
    eng.tick()
    assert len(b.get_open_orders()) == 1
    eng.shutdown()
    assert b.get_open_orders() == []


from autotrader.domain import OrderRequest


def test_tick_sell_uses_position_qty_not_order_qty(tmp_path):
    """A SELL exit must liquidate the full position, not just order_qty shares."""
    b = SimBroker(quotes={"US.AAPL": 94.0}, cash=100000.0)
    # Create a position of 10 shares at avg_price=120 via a direct limit order.
    # stop_loss_pct=0.05 -> stop at 120*0.95=114; quote 94 < 114 -> stop triggers.
    b.place_order(OrderRequest(symbol="US.AAPL", side="BUY", qty=10,
                               order_type="LIMIT", limit_price=120.0,
                               client_order_id="setup-cid"))
    strat = ThresholdStrategy(StrategyParams(symbol="US.AAPL", entry_price=999.0,
                                             stop_loss_pct=0.05, take_profit_pct=0.10,
                                             confidence=0.7))
    # order_qty=1, position=10 — the SELL should be for 10, not 1.
    eng = TradeEngine(broker=b, strategy=strat, cfg=_cfg(), order_qty=1,
                      audit_path=str(tmp_path / "audit.jsonl"))
    result = eng.tick()
    assert result.action == "ORDER_PLACED"
    all_fills = b.reconcile_fills(None)
    sell_fills = [f for f in all_fills if f.side == "SELL"]
    assert len(sell_fills) == 1, "exactly one SELL fill expected"
    assert sell_fills[0].qty == 10, f"expected qty 10 (position qty), got {sell_fills[0].qty}"


def test_tick_records_signal_and_trade_to_db(tmp_path):
    """When a DB is injected, tick() writes the signal and trade to it."""
    from autotrader.db import DB
    db = DB(str(tmp_path / "autotrader.db"))
    b = SimBroker(quotes={"US.AAPL": 101.0}, cash=100000.0)
    strat = ThresholdStrategy(StrategyParams(symbol="US.AAPL", entry_price=100.0,
                                             stop_loss_pct=0.05, take_profit_pct=0.10,
                                             confidence=0.7))
    eng = TradeEngine(broker=b, strategy=strat, cfg=_cfg(), order_qty=10,
                      audit_path=str(tmp_path / "audit.jsonl"), db=db)
    result = eng.tick()
    assert result.action == "ORDER_PLACED"
    sig_count = db._conn.execute("SELECT COUNT(*) FROM signals").fetchone()[0]
    assert sig_count == 1, "signal must be recorded"
    trade_count = db._conn.execute("SELECT COUNT(*) FROM trades").fetchone()[0]
    assert trade_count == 1, "trade must be recorded"
    perf_count = db._conn.execute("SELECT COUNT(*) FROM performance").fetchone()[0]
    assert perf_count == 1, "performance snapshot must be recorded"
    db.close()
