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
    # W7: performance rows are written ONLY by SessionRunner._record_perf
    # (RISK_CHECK/RISK_SWEEP/EOD jobs) — the engine's signal route never
    # writes one, so a bare tick() must leave the table empty.
    perf_count = db._conn.execute("SELECT COUNT(*) FROM performance").fetchone()[0]
    assert perf_count == 0, "engine route must not record a performance snapshot"
    db.close()


def test_entry_gate_blocks_buy_when_closed(tmp_path):
    from autotrader.lifecycle import EntryGate
    b = SimBroker(quotes={"US.AAPL": 101.0}, cash=100000.0)
    strat = ThresholdStrategy(StrategyParams(symbol="US.AAPL", entry_price=100.0,
                                             stop_loss_pct=0.05, take_profit_pct=0.10,
                                             confidence=0.7))
    eng = TradeEngine(broker=b, strategy=strat, cfg=_cfg(), order_qty=10,
                      audit_path=str(tmp_path / "audit.jsonl"),
                      entry_gate=EntryGate(enabled=False))
    result = eng.tick()
    assert result.action == "ENTRY_CLOSED"
    assert b.get_account().position_qty("US.AAPL") == 0


def test_entry_gate_allows_buy_when_open(tmp_path):
    from autotrader.lifecycle import EntryGate
    b = SimBroker(quotes={"US.AAPL": 101.0}, cash=100000.0)
    strat = ThresholdStrategy(StrategyParams(symbol="US.AAPL", entry_price=100.0,
                                             stop_loss_pct=0.05, take_profit_pct=0.10,
                                             confidence=0.7))
    eng = TradeEngine(broker=b, strategy=strat, cfg=_cfg(), order_qty=10,
                      audit_path=str(tmp_path / "audit.jsonl"),
                      entry_gate=EntryGate(enabled=True))
    assert eng.tick().action == "ORDER_PLACED"


def test_entry_gate_allows_sell_exit_even_when_closed(tmp_path):
    from autotrader.lifecycle import EntryGate
    from autotrader.domain import OrderRequest
    b = SimBroker(quotes={"US.AAPL": 94.0}, cash=100000.0)
    b.place_order(OrderRequest(symbol="US.AAPL", side="BUY", qty=10,
                               order_type="LIMIT", limit_price=120.0,
                               client_order_id="setup-cid"))
    strat = ThresholdStrategy(StrategyParams(symbol="US.AAPL", entry_price=999.0,
                                             stop_loss_pct=0.05, take_profit_pct=0.10,
                                             confidence=0.7))
    eng = TradeEngine(broker=b, strategy=strat, cfg=_cfg(), order_qty=1,
                      audit_path=str(tmp_path / "audit.jsonl"),
                      entry_gate=EntryGate(enabled=False))  # entries CLOSED
    # quote 94 < stop 114 -> SELL exit; gate must NOT block exits
    assert eng.tick().action == "ORDER_PLACED"


def test_submit_external_signal_routes_and_places(tmp_path):
    from autotrader.domain import Signal
    b = SimBroker(quotes={"US.AAPL": 101.0}, cash=100000.0)
    eng = _engine(b, tmp_path=tmp_path)   # no entry gate -> BUY allowed
    res = eng.submit_external_signal(
        Signal(symbol="US.AAPL", direction="BUY", confidence=0.9, rationale="ext"))
    assert res.action == "ORDER_PLACED"
    assert b.get_account().position_qty("US.AAPL") == 10


def test_submit_external_signal_deferred_by_entry_gate(tmp_path):
    # A pre-market external BUY is deferred (held for the open), not dropped —
    # nothing is placed yet, but the signal is not lost. See test_deferred_entries.
    from autotrader.domain import Signal
    from autotrader.lifecycle import EntryGate
    b = SimBroker(quotes={"US.AAPL": 101.0}, cash=100000.0)
    strat = ThresholdStrategy(StrategyParams(symbol="US.AAPL", entry_price=100.0,
                                             stop_loss_pct=0.05, take_profit_pct=0.10,
                                             confidence=0.7))
    eng = TradeEngine(broker=b, strategy=strat, cfg=_cfg(), order_qty=10,
                      audit_path=str(tmp_path / "audit.jsonl"),
                      entry_gate=EntryGate(enabled=False))
    res = eng.submit_external_signal(
        Signal(symbol="US.AAPL", direction="BUY", confidence=0.9, rationale="ext"))
    assert res.action == "ENTRY_DEFERRED"
    assert b.get_account().position_qty("US.AAPL") == 0


def test_submit_external_signal_drops_low_confidence(tmp_path):
    from autotrader.domain import Signal
    b = SimBroker(quotes={"US.AAPL": 101.0}, cash=100000.0)
    eng = _engine(b, cfg=_cfg(min_confidence=0.9), tmp_path=tmp_path)
    res = eng.submit_external_signal(
        Signal(symbol="US.AAPL", direction="BUY", confidence=0.5, rationale="ext"))
    assert res.action == "DROPPED_LOW_CONFIDENCE"


def test_buy_entry_attaches_trailing_stop(tmp_path):
    from autotrader.db import DB
    db = DB(str(tmp_path / "stops.db"))
    b = SimBroker(quotes={"US.AAPL": 101.0}, cash=100000.0)
    strat = ThresholdStrategy(StrategyParams(symbol="US.AAPL", entry_price=100.0,
                                             stop_loss_pct=0.05, take_profit_pct=0.10,
                                             confidence=0.7))
    eng = TradeEngine(broker=b, strategy=strat, cfg=_cfg(trailing_stop_pct=5.0),
                      order_qty=10, audit_path=str(tmp_path / "audit.jsonl"), db=db)
    assert eng.tick().action == "ORDER_PLACED"
    assert b.get_account().position_qty("US.AAPL") == 10     # BUY filled
    assert len(b.get_open_orders()) == 1                     # the resting trailing stop
    row = db._conn.execute(
        "SELECT side, qty FROM trades WHERE order_type='TRAILING_STOP'").fetchone()
    assert row is not None and row[0] == "SELL" and row[1] == 10
    db.close()


def test_no_trailing_stop_when_disabled(tmp_path):
    b = SimBroker(quotes={"US.AAPL": 101.0}, cash=100000.0)
    eng = _engine(b, tmp_path=tmp_path)   # _cfg() default trailing_stop_pct=0.0
    assert eng.tick().action == "ORDER_PLACED"
    assert b.get_open_orders() == []      # no resting stop when disabled


def _ext_engine(broker, cfg, tmp_path, qty=1):
    """Engine for external-signal sizing tests (no strategy auto-signal needed)."""
    strat = ThresholdStrategy(StrategyParams(symbol="US.AAPL", entry_price=999.0,
                                             stop_loss_pct=0.05, take_profit_pct=0.10,
                                             confidence=0.7))
    return TradeEngine(broker=broker, strategy=strat, cfg=cfg, order_qty=qty,
                       audit_path=str(tmp_path / "audit.jsonl"))


def test_buy_uses_risk_sizing_when_enabled(tmp_path):
    """With sizing on and a signal-carried stop, BUY qty is risk-derived, not order_qty."""
    from autotrader.domain import Signal
    # equity ~= total_assets = cash 100k; risk 1% = $1000; price 100, stop 98 -> dist 2;
    # base 500; confidence 1.0 -> 500 shares (order_qty is only 1).
    b = SimBroker(quotes={"US.AAPL": 100.0}, cash=100000.0)
    cfg = _cfg(risk_per_trade_pct=0.01, max_position_qty=10000, max_order_notional=1e9,
               max_gross_exposure=1e9)
    eng = _ext_engine(b, cfg, tmp_path, qty=1)
    res = eng.submit_external_signal(
        Signal(symbol="US.AAPL", direction="BUY", confidence=1.0, rationale="ext",
               stop_price=98.0))
    assert res.action == "ORDER_PLACED"
    assert b.get_account().position_qty("US.AAPL") == 500


def test_buy_falls_back_to_fixed_when_no_stop_and_no_trailing(tmp_path):
    from autotrader.domain import Signal
    b = SimBroker(quotes={"US.AAPL": 100.0}, cash=100000.0)
    cfg = _cfg(risk_per_trade_pct=0.01, trailing_stop_pct=0.0)  # sizing on, but no stop source
    eng = _ext_engine(b, cfg, tmp_path, qty=4)
    res = eng.submit_external_signal(
        Signal(symbol="US.AAPL", direction="BUY", confidence=1.0, rationale="ext"))
    assert res.action == "ORDER_PLACED"
    assert b.get_account().position_qty("US.AAPL") == 4   # fixed ORDER_QTY fallback


def test_buy_sized_zero_places_nothing(tmp_path):
    from autotrader.domain import Signal
    b = SimBroker(quotes={"US.AAPL": 100.0}, cash=100.0)   # tiny equity
    cfg = _cfg(risk_per_trade_pct=0.0001, trailing_stop_pct=5.0)  # base floors to 0
    eng = _ext_engine(b, cfg, tmp_path, qty=1)
    res = eng.submit_external_signal(
        Signal(symbol="US.AAPL", direction="BUY", confidence=1.0, rationale="ext"))
    assert res.action == "SIZED_ZERO"
    assert b.get_account().position_qty("US.AAPL") == 0


def test_sell_still_full_liquidation_with_sizing_enabled(tmp_path):
    """Sizing is BUY-only; a SELL exit still liquidates the whole position."""
    from autotrader.domain import OrderRequest, Signal
    b = SimBroker(quotes={"US.AAPL": 94.0}, cash=100000.0)
    b.place_order(OrderRequest(symbol="US.AAPL", side="BUY", qty=10,
                               order_type="LIMIT", limit_price=120.0,
                               client_order_id="setup-cid"))
    cfg = _cfg(risk_per_trade_pct=0.01, trailing_stop_pct=5.0)
    eng = _ext_engine(b, cfg, tmp_path, qty=1)
    res = eng.submit_external_signal(
        Signal(symbol="US.AAPL", direction="SELL", confidence=1.0, rationale="exit"))
    assert res.action == "ORDER_PLACED"
    sell_fills = [f for f in b.reconcile_fills(None) if f.side == "SELL"]
    assert len(sell_fills) == 1 and sell_fills[0].qty == 10   # full position, not sized


def test_sized_qty_clamped_to_pass_risk_core(tmp_path):
    """A risk-base above the position cap is clamped DOWN so risk_core still approves."""
    from autotrader.domain import Signal
    b = SimBroker(quotes={"US.AAPL": 100.0}, cash=100000.0)
    # base would be 500, but max_position_qty caps the order at 50.
    cfg = _cfg(risk_per_trade_pct=0.01, max_position_qty=50, max_order_notional=1e9,
               max_gross_exposure=1e9)
    eng = _ext_engine(b, cfg, tmp_path, qty=1)
    res = eng.submit_external_signal(
        Signal(symbol="US.AAPL", direction="BUY", confidence=1.0, rationale="ext",
               stop_price=98.0))
    assert res.action == "ORDER_PLACED"
    assert b.get_account().position_qty("US.AAPL") == 50
