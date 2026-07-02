"""SessionRunner ties scheduler + lifecycle jobs + watchdog + engine into one
loop. run_once(now) executes a single iteration; tests drive it with a
FixedClock and assert job side-effects, DB writes, and tick outcomes. No real
sleeping or OpenD."""
from datetime import datetime
from zoneinfo import ZoneInfo

from autotrader.clock import FixedClock
from autotrader.config import RiskConfig
from autotrader.db import DB
from autotrader.lifecycle import EntryGate
from autotrader.scheduler import LifecycleScheduler
from autotrader.sim_broker import SimBroker
from autotrader.strategies.threshold import ThresholdStrategy, StrategyParams
from autotrader.watchdog import Watchdog
from autotrader.main import TradeEngine
from autotrader.runner import SessionRunner
from autotrader.domain import OrderRequest

_NY = ZoneInfo("America/New_York")


def _cfg(**over):
    base = dict(trading_env="PAPER", min_confidence=0.6, max_order_notional=20000,
                max_position_qty=100, daily_loss_limit=500, max_gross_exposure=100000,
                allowed_symbols=frozenset({"US.AAPL"}))
    base.update(over)
    return RiskConfig(**base)


def _dt(h, m, day=12):
    return datetime(2026, 6, day, h, m, tzinfo=_NY)


def _build(tmp_path, broker, *, healthy=True, gate_enabled=False, inbox=None, reporter=None):
    db = DB(str(tmp_path / "runner.db"))
    gate = EntryGate(enabled=gate_enabled)
    strat = ThresholdStrategy(StrategyParams(symbol="US.AAPL", entry_price=100.0,
                                             stop_loss_pct=0.05, take_profit_pct=0.10,
                                             confidence=0.7))
    eng = TradeEngine(broker=broker, strategy=strat, cfg=_cfg(), order_qty=10,
                      audit_path=str(tmp_path / "audit.jsonl"), db=db, entry_gate=gate)
    watch = Watchdog(health_check=lambda: healthy, reconcile=lambda: None,
                     sleep=lambda s: None)
    runner = SessionRunner(engine=eng, broker=broker, db=db, gate=gate,
                           scheduler=LifecycleScheduler(), watchdog=watch,
                           clock=FixedClock(_dt(8, 0)), sleep=lambda s: None,
                           signal_inbox=inbox, reporter=reporter)
    return runner, db, gate


def test_run_once_pre_open_sync_writes_positions(tmp_path):
    b = SimBroker(quotes={"US.AAPL": 101.0}, cash=100000.0)
    b.place_order(OrderRequest(symbol="US.AAPL", side="BUY", qty=7,
                               order_type="LIMIT", limit_price=150.0,
                               client_order_id="seed"))
    runner, db, gate = _build(tmp_path, b)
    runner.run_once(_dt(8, 31))   # fires PRE_OPEN_SYNC
    pos = db._conn.execute("SELECT qty FROM positions WHERE symbol='US.AAPL'").fetchone()
    assert pos is not None and pos[0] == 7
    db.close()


def test_run_once_entry_open_enables_then_buy_places(tmp_path):
    b = SimBroker(quotes={"US.AAPL": 101.0}, cash=100000.0)
    runner, db, gate = _build(tmp_path, b)
    # Before 09:45 the gate is closed -> a BUY signal is ENTRY_CLOSED
    assert runner.run_once(_dt(8, 31)) == "ENTRY_CLOSED"
    # At/after 09:45 ENTRY_OPEN fires, gate opens, the BUY is placed
    assert runner.run_once(_dt(9, 46)) == "ORDER_PLACED"
    assert gate.entries_enabled is True
    db.close()


def test_run_once_risk_sweep_closes_entries_and_records_perf(tmp_path):
    b = SimBroker(quotes={"US.AAPL": 101.0}, cash=100000.0)
    runner, db, gate = _build(tmp_path, b)
    runner.run_once(_dt(9, 46))           # open entries
    runner.run_once(_dt(15, 31))          # RISK_SWEEP
    assert gate.entries_enabled is False
    perf = db._conn.execute("SELECT COUNT(*) FROM performance").fetchone()[0]
    assert perf >= 1
    db.close()


def test_run_once_eod_flatten_cancels_all(tmp_path):
    b = SimBroker(quotes={"US.AAPL": 101.0}, cash=100000.0, auto_fill=False)
    runner, db, gate = _build(tmp_path, b, gate_enabled=True)
    runner.run_once(_dt(10, 0))           # places a working (unfilled) order
    assert len(b.get_open_orders()) == 1
    runner.run_once(_dt(16, 16))          # EOD_FLATTEN cancels all
    assert b.get_open_orders() == []
    assert gate.entries_enabled is False
    db.close()


def test_run_once_halts_when_watchdog_unhealthy(tmp_path):
    b = SimBroker(quotes={"US.AAPL": 101.0}, cash=100000.0)
    runner, db, gate = _build(tmp_path, b, healthy=False)
    # Past the morning jobs; watchdog can't recover -> loop reports halt, no tick.
    assert runner.run_once(_dt(10, 0)) == "HALTED_UNHEALTHY"
    db.close()


def test_run_loops_until_stop(tmp_path):
    b = SimBroker(quotes={"US.AAPL": 99.0}, cash=100000.0)  # below entry -> NO_SIGNAL
    runner, db, gate = _build(tmp_path, b)
    calls = {"n": 0}

    def stop():
        calls["n"] += 1
        return calls["n"] > 3   # allow 3 iterations then stop

    runner.run(stop=stop)
    assert calls["n"] == 4, "stop polled until it returned True"
    db.close()


def test_run_once_routes_external_signal_from_inbox(tmp_path):
    import json
    from autotrader.signals.inbox import SignalInbox
    inbox_dir = tmp_path / "inbox"
    inbox_dir.mkdir()
    payload = {
        "routine_id": "r1", "timestamp": "2026-06-12T09:46:00-04:00",
        "signal_changes": [
            {"ticker": "AAPL", "direction": "UP", "transition": ["50", "200"],
             "points_delta": 10, "driver": "breakout"}
        ],
    }
    (inbox_dir / "sig1.json").write_text(json.dumps(payload))
    # Quote 99 < entry 100 -> the STRATEGY emits NO_SIGNAL; only the external BUY acts.
    b = SimBroker(quotes={"US.AAPL": 99.0}, cash=100000.0)
    runner, db, gate = _build(tmp_path, b, inbox=SignalInbox(str(inbox_dir)))
    runner.run_once(_dt(9, 46))   # ENTRY_OPEN fires -> gate opens -> external BUY routes
    assert b.get_account().position_qty("US.AAPL") == 10
    db.close()


def test_pre_market_external_buy_is_deferred_then_placed_at_entry_open(tmp_path):
    import json
    from autotrader.signals.inbox import SignalInbox
    inbox_dir = tmp_path / "inbox"
    inbox_dir.mkdir()
    payload = {
        "routine_id": "r1", "timestamp": "2026-06-12T07:20:00-04:00",
        "signal_changes": [
            {"ticker": "AAPL", "direction": "UP", "transition": ["HOLD", "BUY"],
             "points_delta": 10, "driver": "ticker sweep"}
        ],
    }
    (inbox_dir / "sig1.json").write_text(json.dumps(payload))
    # Quote 99 < entry 100 -> strategy stays NO_SIGNAL; only the external BUY acts.
    b = SimBroker(quotes={"US.AAPL": 99.0}, cash=100000.0)
    runner, db, gate = _build(tmp_path, b, inbox=SignalInbox(str(inbox_dir)))

    # Pre-market: the drop is consumed but entries are closed -> deferred, not placed.
    runner.run_once(_dt(8, 31))
    assert b.get_account().position_qty("US.AAPL") == 0
    assert not (inbox_dir / "sig1.json").exists()   # inbox consumed the drop (once)

    # ENTRY_OPEN (09:45): the runner opens the gate and flushes the deferred BUY.
    runner.run_once(_dt(9, 46))
    assert b.get_account().position_qty("US.AAPL") == 10
    db.close()


def test_run_once_skips_inbox_when_unhealthy(tmp_path):
    import json
    from autotrader.signals.inbox import SignalInbox
    inbox_dir = tmp_path / "inbox"
    inbox_dir.mkdir()
    payload = {"routine_id": "r1", "timestamp": "2026-06-12T10:00:00-04:00",
               "signal_changes": [{"ticker": "AAPL", "direction": "UP",
                                   "transition": [], "points_delta": 10, "driver": "x"}]}
    (inbox_dir / "sig1.json").write_text(json.dumps(payload))
    b = SimBroker(quotes={"US.AAPL": 99.0}, cash=100000.0)
    runner, db, gate = _build(tmp_path, b, healthy=False, inbox=SignalInbox(str(inbox_dir)))
    assert runner.run_once(_dt(10, 0)) == "HALTED_UNHEALTHY"
    assert b.get_account().position_qty("US.AAPL") == 0   # no external order placed
    assert (inbox_dir / "sig1.json").exists()             # file NOT consumed while halted
    db.close()


class _FakeReporter:
    def __init__(self):
        self.calls = []

    def send_eod_report(self, now):
        self.calls.append(now)


def test_eod_report_job_invokes_reporter(tmp_path):
    b = SimBroker(quotes={"US.AAPL": 101.0}, cash=100000.0)
    rep = _FakeReporter()
    runner, db, gate = _build(tmp_path, b, reporter=rep)
    runner.run_once(_dt(16, 31))            # first poll catches up through EOD_REPORT
    assert len(rep.calls) == 1
    assert rep.calls[0] == _dt(16, 31)
    db.close()


def test_eod_report_without_reporter_is_noop(tmp_path):
    b = SimBroker(quotes={"US.AAPL": 101.0}, cash=100000.0)
    runner, db, gate = _build(tmp_path, b)   # reporter defaults to None
    runner.run_once(_dt(16, 31))            # must not raise
    db.close()


def test_engine_routes_never_overwrite_performance_row(tmp_path):
    """W7: the runner's fills-derived EOD write is authoritative; a signal
    routed afterwards (e.g. a post-16:15 stop SELL — SELLs are never gated)
    must not replace it with raw broker day_pnl."""
    b = SimBroker(quotes={"US.AAPL": 101.0}, cash=100000.0)
    runner, db, gate = _build(tmp_path, b)
    runner.run_once(_dt(9, 46))            # ENTRY_OPEN + BUY placed
    runner.run_once(_dt(16, 16))           # EOD job: authoritative perf write
    before = db._conn.execute(
        "SELECT day_pnl, updated_at FROM performance").fetchone()
    # a SELL routed after the EOD write (engine path)
    from autotrader.domain import Signal
    runner._engine.submit_external_signal(
        Signal(symbol="US.AAPL", direction="SELL", confidence=0.9, rationale="stop"))
    after = db._conn.execute(
        "SELECT day_pnl, updated_at FROM performance").fetchone()
    assert after == before                 # row untouched by the engine route
    db.close()


def test_risk_check_jobs_record_perf_via_runner(tmp_path):
    b = SimBroker(quotes={"US.AAPL": 90.0}, cash=100000.0)   # 90 < 100: NO_SIGNAL
    runner, db, gate = _build(tmp_path, b)
    runner.run_once(_dt(13, 31))           # RISK_CHECK_MID
    row = db._conn.execute("SELECT COUNT(*) FROM performance").fetchone()
    assert row[0] == 1                     # runner wrote it (engine no longer does)
    db.close()
