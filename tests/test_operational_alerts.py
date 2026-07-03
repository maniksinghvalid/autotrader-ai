"""V8b: real-time Slack alerts for a hard risk HALT, watchdog-unhealthy
episodes, and consecutive run_once loop errors, plus EOD fills-sync before
the day's final performance row. Tests exercise AlertSink's REAL dedup
semantics (Task 2) — not mocks of the alerting logic."""
from datetime import datetime

from autotrader.alerts import AlertSink
from autotrader.lifecycle import EntryGate
from autotrader.runner import SessionRunner
from autotrader.scheduler import LifecycleScheduler


class _SickWatch:
    def ensure_healthy(self):
        return False


class _DB:
    def __init__(self):
        self.halts = []
    def record_halt(self, reason):
        self.halts.append(reason)
        return 1
    def get_state(self, k): return None
    def set_state(self, k, v): pass


def _runner(posted, watch, db=None):
    class _Eng:
        def tick(self):
            class R: action = "NO_SIGNAL"
            return R()
    return SessionRunner(engine=_Eng(), broker=None, db=db,
                         gate=EntryGate(enabled=True),
                         scheduler=LifecycleScheduler(), watchdog=watch,
                         clock=None, sleep=lambda s: None,
                         alerts=AlertSink("http://x",
                                          post=lambda u, p: posted.append(p["text"]) or 200))


def test_unhealthy_alerts_once_per_episode_and_records_halt():
    posted, db = [], _DB()
    r = _runner(posted, _SickWatch(), db)
    now = datetime(2026, 7, 6, 10, 0)
    assert r.run_once(now) == "HALTED_UNHEALTHY"
    assert r.run_once(now) == "HALTED_UNHEALTHY"
    watchdog_alerts = [t for t in posted if "watchdog" in t.lower() or "unhealthy" in t.lower()]
    assert len(watchdog_alerts) == 1                     # once per episode
    assert len(db.halts) == 1


def test_unhealthy_episode_resets_and_alerts_again_on_new_outage():
    """The key resets once healthy again, so a SECOND outage alerts again."""
    posted, db = [], _DB()

    class _Flaky:
        def __init__(self):
            self.healthy = False
        def ensure_healthy(self):
            return self.healthy

    watch = _Flaky()
    r = _runner(posted, watch, db)
    now = datetime(2026, 7, 6, 10, 0)
    assert r.run_once(now) == "HALTED_UNHEALTHY"
    watch.healthy = True
    assert r.run_once(now) == "NO_SIGNAL"
    watch.healthy = False
    assert r.run_once(now) == "HALTED_UNHEALTHY"
    watchdog_alerts = [t for t in posted if "unhealthy" in t.lower()]
    assert len(watchdog_alerts) == 2                     # new episode alerts again
    assert len(db.halts) == 2


def test_halt_sends_slack(tmp_path):
    from autotrader.config import RiskConfig
    from autotrader.main import TradeEngine
    from autotrader.sim_broker import SimBroker
    from autotrader.strategies.breakout import BreakoutParams, BreakoutStrategy

    class _LossBroker(SimBroker):
        def get_account(self):
            snap = super().get_account()
            object.__setattr__(snap, "day_pnl", -5000.0)
            return snap

    posted = []
    cfg = RiskConfig(trading_env="PAPER", min_confidence=0.5, max_order_notional=1e6,
                     max_position_qty=1000, daily_loss_limit=500, max_gross_exposure=1e6,
                     allowed_symbols=frozenset(), daily_loss_halt=1000.0)
    strat = BreakoutStrategy(BreakoutParams(symbol="US.T", stop_loss_pct=0.05,
                                            take_profit_pct=0.10, confidence=0.9))
    eng = TradeEngine(_LossBroker({"US.T": 1.0}), strat, cfg, order_qty=1,
                      audit_path=str(tmp_path / "a.jsonl"),
                      alerts=AlertSink("http://x",
                                       post=lambda u, p: posted.append(p["text"]) or 200))
    eng.apply_risk_check(datetime(2026, 7, 6, 13, 30))
    assert any("HALT" in t for t in posted)


def test_halt_alert_fires_once_per_halt_day(tmp_path):
    from autotrader.config import RiskConfig
    from autotrader.main import TradeEngine
    from autotrader.sim_broker import SimBroker
    from autotrader.strategies.breakout import BreakoutParams, BreakoutStrategy

    class _LossBroker(SimBroker):
        def get_account(self):
            snap = super().get_account()
            object.__setattr__(snap, "day_pnl", -5000.0)
            return snap

    posted = []
    cfg = RiskConfig(trading_env="PAPER", min_confidence=0.5, max_order_notional=1e6,
                     max_position_qty=1000, daily_loss_limit=500, max_gross_exposure=1e6,
                     allowed_symbols=frozenset(), daily_loss_halt=1000.0)
    strat = BreakoutStrategy(BreakoutParams(symbol="US.T", stop_loss_pct=0.05,
                                            take_profit_pct=0.10, confidence=0.9))
    eng = TradeEngine(_LossBroker({"US.T": 1.0}), strat, cfg, order_qty=1,
                      audit_path=str(tmp_path / "a.jsonl"),
                      alerts=AlertSink("http://x",
                                       post=lambda u, p: posted.append(p["text"]) or 200))
    now = datetime(2026, 7, 6, 13, 30)
    eng.apply_risk_check(now)
    eng.apply_risk_check(now)   # already halted; a second call must not re-alert
    halt_alerts = [t for t in posted if "HALT" in t]
    assert len(halt_alerts) == 1


def test_run_alerts_on_five_consecutive_loop_errors(tmp_path):
    """run() posts exactly one alert containing 'consecutive' once the
    consecutive run_once exception count reaches 5, driven with a stop that
    allows ~6 iterations."""
    posted = []

    class _BoomEngine:
        def tick(self):
            raise RuntimeError("boom")

    class _AlwaysHealthy:
        def ensure_healthy(self):
            return True

    class _FixedClock:
        def now_est(self):
            return datetime(2026, 7, 6, 10, 0)

    runner = SessionRunner(engine=_BoomEngine(), broker=None, db=None,
                           gate=EntryGate(enabled=True),
                           scheduler=LifecycleScheduler(), watchdog=_AlwaysHealthy(),
                           clock=_FixedClock(), sleep=lambda s: None,
                           alerts=AlertSink("http://x",
                                            post=lambda u, p: posted.append(p["text"]) or 200))
    calls = {"n": 0}

    def stop():
        calls["n"] += 1
        return calls["n"] > 6

    runner.run(stop=stop)
    consecutive_alerts = [t for t in posted if "consecutive" in t.lower()]
    assert len(consecutive_alerts) == 1


def test_run_resets_error_counter_on_clean_iteration(tmp_path):
    """A clean iteration between errors resets the counter, so fewer than 5
    consecutive errors never fires the alert even across many iterations."""
    posted = []

    class _FlakyEngine:
        def __init__(self):
            self.n = 0
        def tick(self):
            self.n += 1
            if self.n % 2 == 0:
                raise RuntimeError("boom")
            class R: action = "NO_SIGNAL"
            return R()

    class _AlwaysHealthy:
        def ensure_healthy(self):
            return True

    class _FixedClock:
        def now_est(self):
            return datetime(2026, 7, 6, 10, 0)

    runner = SessionRunner(engine=_FlakyEngine(), broker=None, db=None,
                           gate=EntryGate(enabled=True),
                           scheduler=LifecycleScheduler(), watchdog=_AlwaysHealthy(),
                           clock=_FixedClock(), sleep=lambda s: None,
                           alerts=AlertSink("http://x",
                                            post=lambda u, p: posted.append(p["text"]) or 200))
    calls = {"n": 0}

    def stop():
        calls["n"] += 1
        return calls["n"] > 20

    runner.run(stop=stop)
    consecutive_alerts = [t for t in posted if "consecutive" in t.lower()]
    assert len(consecutive_alerts) == 0


def test_eod_cancel_orders_syncs_fills_before_recording_perf(tmp_path):
    """I6: fills between 15:30-16:30 (RISK_SWEEP already ran) must land in the
    DB's fills table and the day's FINAL performance row before EOD_CANCEL_ORDERS
    records perf — not just cancel orders and record stale figures."""
    from autotrader.config import RiskConfig
    from autotrader.db import DB
    from autotrader.domain import OrderRequest
    from autotrader.main import TradeEngine
    from autotrader.sim_broker import SimBroker
    from autotrader.strategies.threshold import StrategyParams, ThresholdStrategy
    from autotrader.watchdog import Watchdog

    db = DB(str(tmp_path / "eod.db"))
    b = SimBroker(quotes={"US.AAPL": 101.0}, cash=100000.0)
    gate = EntryGate(enabled=True)
    cfg = RiskConfig(trading_env="PAPER", min_confidence=0.6, max_order_notional=20000,
                     max_position_qty=100, daily_loss_limit=500, max_gross_exposure=100000,
                     allowed_symbols=frozenset({"US.AAPL"}))
    strat = ThresholdStrategy(StrategyParams(symbol="US.AAPL", entry_price=100.0,
                                             stop_loss_pct=0.05, take_profit_pct=0.10,
                                             confidence=0.7))
    eng = TradeEngine(broker=b, strategy=strat, cfg=cfg, order_qty=10,
                      audit_path=str(tmp_path / "audit.jsonl"), db=db, entry_gate=gate)
    watch = Watchdog(health_check=lambda: True, reconcile=lambda: None, sleep=lambda s: None)
    runner = SessionRunner(engine=eng, broker=b, db=db, gate=gate,
                           scheduler=LifecycleScheduler(), watchdog=watch,
                           clock=None, sleep=lambda s: None)

    # A fill that lands AFTER RISK_SWEEP (15:30) but before EOD_CANCEL_ORDERS
    # (16:15) — simulating the 15:30-16:30 window in I6.
    b.place_order(OrderRequest(symbol="US.AAPL", side="BUY", qty=5,
                               order_type="LIMIT", limit_price=150.0,
                               client_order_id="late-fill"))
    assert db._conn.execute("SELECT COUNT(*) FROM fills").fetchone()[0] == 0
    from datetime import datetime as _dt
    runner._run_job("EOD_FLATTEN", _dt(2026, 7, 6, 16, 15))
    assert db._conn.execute("SELECT COUNT(*) FROM fills").fetchone()[0] == 1
    db.close()
