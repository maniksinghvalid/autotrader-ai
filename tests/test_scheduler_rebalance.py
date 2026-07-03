from datetime import datetime

from autotrader.scheduler import (
    LifecycleScheduler, REBALANCE, RISK_CHECK_MID, RISK_CHECK_LATE,
    PRE_OPEN_SYNC, ENTRY_OPEN, RISK_SWEEP, EOD_FLATTEN,
)


def _poll_and_mark(s, now):
    """poll(), then mark_fired every returned job — simulates every due job
    succeeding, exactly as SessionRunner.run_once does on the happy path
    (poll() itself no longer self-marks; see test_scheduler_mark_on_success.py)."""
    due = s.poll(now)
    for name in due:
        s.mark_fired(name, now)
    return due


def test_new_jobs_fire_at_their_times():
    s = LifecycleScheduler()
    due = s.poll(datetime(2026, 6, 16, 12, 35))
    assert due == [PRE_OPEN_SYNC, ENTRY_OPEN, REBALANCE]


def test_risk_checks_fire_once_each():
    s = LifecycleScheduler()
    assert RISK_CHECK_MID in _poll_and_mark(s, datetime(2026, 6, 16, 13, 30))
    assert s.poll(datetime(2026, 6, 16, 13, 45)) == []
    assert RISK_CHECK_LATE in s.poll(datetime(2026, 6, 16, 15, 0))


def test_full_day_order():
    s = LifecycleScheduler()
    due = s.poll(datetime(2026, 6, 16, 16, 20))
    assert due == [PRE_OPEN_SYNC, ENTRY_OPEN, REBALANCE, RISK_CHECK_MID,
                   RISK_CHECK_LATE, RISK_SWEEP, EOD_FLATTEN]
