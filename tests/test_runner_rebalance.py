from datetime import datetime

from autotrader.lifecycle import EntryGate
from autotrader.runner import SessionRunner
from autotrader.scheduler import LifecycleScheduler


class _Eng:
    def __init__(self):
        self.calls = []

    def tick(self):
        from autotrader.main import TickResult
        self.calls.append("tick")
        return TickResult("NO_SIGNAL")

    def rebalance(self, now):
        self.calls.append(("rebalance", now))
        return "REBALANCED"

    def apply_risk_check(self, now):
        self.calls.append(("risk", now))
        return "OK"


class _Watch:
    def ensure_healthy(self):
        return True


def _runner(eng, gate):
    return SessionRunner(engine=eng, broker=None, db=None, gate=gate,
                         scheduler=LifecycleScheduler(), watchdog=_Watch(),
                         clock=None, sleep=lambda s: None)


def test_rebalance_and_risk_jobs_dispatch():
    eng, gate = _Eng(), EntryGate(enabled=False)
    runner = _runner(eng, gate)
    runner.run_once(datetime(2026, 6, 16, 16, 20))
    kinds = [c[0] if isinstance(c, tuple) else c for c in eng.calls]
    assert "rebalance" in kinds
    assert "risk" in kinds


def test_halt_stops_tick():
    eng, gate = _Eng(), EntryGate(enabled=True)
    gate.halt()
    runner = _runner(eng, gate)
    out = runner.run_once(datetime(2026, 6, 16, 9, 50))
    assert out == "HALTED"
    assert "tick" not in eng.calls
