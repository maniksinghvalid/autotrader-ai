"""LifecycleScheduler fires each daily job once, in chronological order, the
first time poll() is called at/after its NY time. Past-due jobs catch up."""
from datetime import datetime
from zoneinfo import ZoneInfo

_NY = ZoneInfo("America/New_York")


def _dt(h, m, day=12):
    return datetime(2026, 6, day, h, m, tzinfo=_NY)


def test_midday_start_catches_up_past_due_jobs_in_order():
    from autotrader.scheduler import (
        LifecycleScheduler, PRE_OPEN_SYNC, ENTRY_OPEN,
    )
    s = LifecycleScheduler()
    # First poll at 10:00 — 08:30 and 09:45 are both past due, 15:30/16:15 not.
    due = s.poll(_dt(10, 0))
    assert due == [PRE_OPEN_SYNC, ENTRY_OPEN], "past-due jobs fire in chronological order"


def test_each_job_fires_only_once_per_day():
    from autotrader.scheduler import LifecycleScheduler, PRE_OPEN_SYNC
    s = LifecycleScheduler()
    assert PRE_OPEN_SYNC in s.poll(_dt(8, 31))
    assert s.poll(_dt(8, 32)) == [], "already-fired jobs do not re-fire same day"


def test_single_job_fires_at_its_time():
    from autotrader.scheduler import LifecycleScheduler, RISK_SWEEP
    s = LifecycleScheduler()
    s.poll(_dt(10, 0))            # fire the two morning jobs
    assert s.poll(_dt(15, 31)) == [RISK_SWEEP]


def test_eod_flatten_fires_after_1615():
    from autotrader.scheduler import LifecycleScheduler, EOD_FLATTEN
    s = LifecycleScheduler()
    s.poll(_dt(16, 0))           # nothing new yet beyond morning catch-up
    assert EOD_FLATTEN in s.poll(_dt(16, 16))


def test_new_day_resets_all_jobs():
    from autotrader.scheduler import LifecycleScheduler, PRE_OPEN_SYNC
    s = LifecycleScheduler()
    s.poll(_dt(8, 31, day=12))   # fire PRE_OPEN_SYNC on the 12th
    assert s.poll(_dt(8, 31, day=12)) == []
    assert PRE_OPEN_SYNC in s.poll(_dt(8, 31, day=13)), "next day re-arms the jobs"
