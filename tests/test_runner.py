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


def _build(tmp_path, broker, *, healthy=True, gate_enabled=False):
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
                           clock=FixedClock(_dt(8, 0)), sleep=lambda s: None)
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
