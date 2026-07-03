from autotrader.lifecycle import EntryGate
from autotrader.runner import SessionRunner
from autotrader.scheduler import LifecycleScheduler
from datetime import datetime


class _Clock:
    def now_est(self):
        return datetime(2026, 7, 6, 10, 0)


class _Watch:
    def ensure_healthy(self):
        return True


class _Eng:
    def tick(self):
        class R: action = "NO_SIGNAL"
        return R()


def test_heartbeat_fires_on_schedule_and_survives_errors():
    beats = []
    def beat():
        beats.append(1)
        if len(beats) == 2:
            raise OSError("heartbeat endpoint down")   # must not kill the loop
    n = {"i": 0}
    def stop():
        n["i"] += 1
        return n["i"] > 7
    r = SessionRunner(engine=_Eng(), broker=None, db=None,
                      gate=EntryGate(enabled=True), scheduler=LifecycleScheduler(),
                      watchdog=_Watch(), clock=_Clock(), sleep=lambda s: None,
                      heartbeat=beat, heartbeat_every=3)
    r.run(stop=stop)
    assert len(beats) == 3          # iterations 1, 4, 7
