from datetime import datetime, date

from autotrader.lifecycle import EntryGate
from autotrader.runner import SessionRunner
from autotrader.scheduler import LifecycleScheduler


class _Recorder:
    def __init__(self):
        self.calls = []
    def reconcile(self, today):
        self.calls.append(today)


class _Engine:
    def apply_risk_check(self, now):
        return "OK"
    def tick(self):
        class R: action = "NO_SIGNAL"
        return R()
    def flush_deferred_entries(self):
        pass
    def rebalance(self, now):
        pass


class _Watch:
    def ensure_healthy(self):
        return True


class _Broker:
    def get_account(self):
        class Snap:
            positions = []
            positions_loaded = True
            gross_exposure_value = 0
            day_pnl = 0
            total_assets = 100000
            cash = 100000
            unrealized_pnl = 0
            def gross_exposure(self):
                return 0
        return Snap()
    def get_quote(self, symbol):
        return 100.0
    def cancel_all(self):
        pass
    def reconcile_fills(self, since):
        return []
    def get_open_orders(self):
        return []


class _DB:
    _conn = None
    def replace_positions(self, p): pass
    def record_fills(self, f): return 0
    def open_trailing_stop_ids(self): return []
    def get_state(self, k): return None
    def set_state(self, k, v): pass
    def record_performance(self, *a, **k): pass
    class _Conn:
        def execute(self, q):
            class R:
                def fetchall(self):
                    return []
            return R()
    _conn = _Conn()


def _runner(sm, gate=None):
    return SessionRunner(engine=_Engine(), broker=_Broker(), db=_DB(),
                         gate=gate or EntryGate(enabled=True),
                         scheduler=LifecycleScheduler(), watchdog=_Watch(),
                         clock=None, sleep=lambda s: None, stop_manager=sm)


def test_mid_risk_check_runs_stop_reconcile():
    sm = _Recorder()
    _runner(sm).run_once(datetime(2026, 7, 6, 13, 31))  # PRE_OPEN..RISK_CHECK_MID due
    assert date(2026, 7, 6) in sm.calls


def test_halted_gate_skips_reconcile():
    sm = _Recorder()
    gate = EntryGate(enabled=True)
    gate.halt()
    _runner(sm, gate).run_once(datetime(2026, 7, 6, 13, 31))
    assert sm.calls == []
