"""StopManager (spec W1): every held long must have exactly one working
trailing stop each morning; orphaned orders (stops for closed positions,
leftover option legs) are cancelled. Unknown broker state -> logged no-op."""
from datetime import date

from autotrader.config import RiskConfig
from autotrader.db import DB
from autotrader.domain import AccountSnapshot, OrderRequest
from autotrader.main import TradeEngine
from autotrader.sim_broker import SimBroker
from autotrader.stops import StopManager
from autotrader.strategies.threshold import ThresholdStrategy, StrategyParams

TODAY = date(2026, 7, 6)


def _cfg(**over):
    base = dict(trading_env="PAPER", min_confidence=0.6, max_order_notional=50000,
                max_position_qty=1000, daily_loss_limit=500, max_gross_exposure=1000000,
                allowed_symbols=frozenset({"US.AAPL"}), trailing_stop_pct=5.0)
    base.update(over)
    return RiskConfig(**base)


def _mgr(tmp_path, broker, cfg=None, alert_fn=None):
    cfg = cfg or _cfg()
    db = DB(str(tmp_path / "sm.db"))
    strat = ThresholdStrategy(StrategyParams(symbol="US.AAPL", entry_price=1e9,
                                             stop_loss_pct=0.05, take_profit_pct=0.10,
                                             confidence=0.7))
    eng = TradeEngine(broker=broker, strategy=strat, cfg=cfg, order_qty=1,
                      audit_path=str(tmp_path / "audit.jsonl"), db=db)
    return StopManager(eng, broker, db, cfg, alert_fn=alert_fn), db, eng


def _seed_long(broker, qty=10):
    broker.place_order(OrderRequest(symbol="US.AAPL", side="BUY", qty=qty,
                                    order_type="MARKET", limit_price=None,
                                    client_order_id="seed"))


def test_unprotected_long_gets_stop_reattached(tmp_path):
    b = SimBroker(quotes={"US.AAPL": 100.0}, cash=100000.0)
    _seed_long(b)
    mgr, db, _ = _mgr(tmp_path, b)
    res = mgr.reconcile(TODAY)
    assert res.attached == 1 and res.orphans_cancelled == 0
    open_orders = b.get_open_orders()
    assert len(open_orders) == 1                       # the resting TRAILING_STOP
    assert db.get_open_trailing_stop("US.AAPL") is not None
    db.close()


def test_reconcile_is_idempotent_same_day(tmp_path):
    b = SimBroker(quotes={"US.AAPL": 100.0}, cash=100000.0)
    _seed_long(b)
    mgr, db, _ = _mgr(tmp_path, b)
    mgr.reconcile(TODAY)
    res2 = mgr.reconcile(TODAY)
    assert res2.attached == 0 and res2.already_protected == 1
    assert len(b.get_open_orders()) == 1               # still exactly one stop
    db.close()


def test_orphaned_stop_for_closed_position_is_cancelled(tmp_path):
    b = SimBroker(quotes={"US.AAPL": 100.0}, cash=100000.0)
    _seed_long(b)
    mgr, db, eng = _mgr(tmp_path, b)
    mgr.reconcile(TODAY)                                # attaches the stop
    b.place_order(OrderRequest(symbol="US.AAPL", side="SELL", qty=10,
                               order_type="MARKET", limit_price=None,
                               client_order_id="flatten"))   # position now gone
    res = mgr.reconcile(TODAY)
    assert res.orphans_cancelled == 1 and res.attached == 0
    assert b.get_open_orders() == []
    assert db.get_open_trailing_stop("US.AAPL") is None
    db.close()


def test_orphaned_option_leg_is_cancelled(tmp_path):
    opt = "US.AAPL260918C110000"
    b = SimBroker(quotes={"US.AAPL": 100.0, opt: 5.0}, cash=100000.0)
    _seed_long(b)
    # a leftover option-leg LIMIT resting from yesterday (non-marketable -> rests)
    ack = b.place_order(OrderRequest(symbol=opt, side="BUY", qty=1,
                                     order_type="LIMIT", limit_price=1.0,
                                     client_order_id="leg-old"))
    mgr, db, _ = _mgr(tmp_path, b)
    db.record_trade(client_order_id="leg-old", symbol=opt, side="BUY", qty=1,
                    order_type="LIMIT", limit_price=1.0,
                    broker_order_id=ack.broker_order_id, state="SUBMITTED")
    res = mgr.reconcile(TODAY)
    assert res.orphans_cancelled == 1
    assert res.attached == 1                            # the AAPL long still gets its stop
    working = {a.broker_order_id for a in b.get_open_orders()}
    assert ack.broker_order_id not in working
    db.close()


def test_unknown_book_is_a_noop(tmp_path):
    b = SimBroker(quotes={"US.AAPL": 100.0}, cash=100000.0)
    _seed_long(b)
    b.fail_open_orders = True
    mgr, db, _ = _mgr(tmp_path, b)
    assert mgr.reconcile(TODAY) is None                 # no attach, no cancel
    assert len(b._open) == 0
    db.close()


def test_rejected_attach_alerts_operator(tmp_path):
    alerts = []
    b = SimBroker(quotes={"US.AAPL": 100.0}, cash=100000.0)
    _seed_long(b)
    # symbol NOT in the allow-list -> risk core rejects the stop -> alert
    mgr, db, _ = _mgr(tmp_path, b, cfg=_cfg(allowed_symbols=frozenset({"US.MSFT"})),
                      alert_fn=alerts.append)
    res = mgr.reconcile(TODAY)
    assert res.attach_failed == 1 and res.attached == 0
    assert len(alerts) == 1 and "US.AAPL" in alerts[0]
    db.close()


def test_broker_rejected_attach_counts_as_failed_and_alerts(tmp_path):
    """Regression: Moomoo paper trading rejects TRAILING_STOP orders outright
    (broker-level REJECTED ack; risk core never sees a problem). Before the
    fix, attach_trailing_stop returned True unconditionally after submit, so
    this counted as 'attached' and the operator was never alerted — silently
    unprotected positions."""
    alerts = []
    b = SimBroker(quotes={"US.AAPL": 100.0}, cash=100000.0)
    b.reject_order_types = {"TRAILING_STOP"}
    _seed_long(b)
    mgr, db, _ = _mgr(tmp_path, b, alert_fn=alerts.append)
    res = mgr.reconcile(TODAY)
    assert res.attach_failed == 1 and res.attached == 0
    assert b.get_open_orders() == []                    # no resting stop was left behind
    assert len(alerts) == 1 and "US.AAPL" in alerts[0]
    db.close()


def test_stale_snapshot_is_a_noop(tmp_path):
    class StaleBroker(SimBroker):
        def get_account(self):
            return AccountSnapshot(cash=100000.0, total_assets=100000.0,
                                    day_pnl=0.0, stale=True,
                                    positions_loaded=False, positions=())

    b = StaleBroker(quotes={"US.AAPL": 100.0}, cash=100000.0)
    _seed_long(b)
    mgr, db, _ = _mgr(tmp_path, b)
    assert mgr.reconcile(TODAY) is None                 # no attach, no cancel
    assert db.get_open_trailing_stop("US.AAPL") is None
    assert b.get_open_orders() == []
    db.close()


def test_no_quote_alerts_operator(tmp_path):
    alerts = []
    b = SimBroker(quotes={"US.AAPL": 100.0}, cash=100000.0)
    _seed_long(b)
    del b._quotes["US.AAPL"]                            # quote now unavailable
    mgr, db, _ = _mgr(tmp_path, b, alert_fn=alerts.append)
    res = mgr.reconcile(TODAY)
    assert res.skipped_no_quote == 1
    assert len(alerts) == 1
    assert "US.AAPL" in alerts[0]
    assert "no quote" in alerts[0].lower() or "UNPROTECTED" in alerts[0]
    db.close()


def test_runner_reconciles_stops_at_entry_open(tmp_path):
    """Full-loop wiring: a carried position with no stop gets one when the
    ENTRY_OPEN job fires (before deferred entries flush)."""
    from datetime import datetime
    from zoneinfo import ZoneInfo
    from autotrader.clock import FixedClock
    from autotrader.lifecycle import EntryGate
    from autotrader.runner import SessionRunner
    from autotrader.scheduler import LifecycleScheduler
    from autotrader.watchdog import Watchdog

    b = SimBroker(quotes={"US.AAPL": 100.0}, cash=100000.0)
    _seed_long(b)
    mgr, db, eng = _mgr(tmp_path, b)
    gate = EntryGate(enabled=False)
    eng._gate = gate   # reuse the engine built by _mgr
    runner = SessionRunner(engine=eng, broker=b, db=db, gate=gate,
                           scheduler=LifecycleScheduler(),
                           watchdog=Watchdog(health_check=lambda: True,
                                             reconcile=lambda: None,
                                             sleep=lambda s: None),
                           clock=FixedClock(datetime(2026, 7, 6, 9, 46,
                                                     tzinfo=ZoneInfo("America/New_York"))),
                           sleep=lambda s: None, stop_manager=mgr)
    runner.run_once(datetime(2026, 7, 6, 9, 46, tzinfo=ZoneInfo("America/New_York")))
    assert db.get_open_trailing_stop("US.AAPL") is not None
    db.close()
