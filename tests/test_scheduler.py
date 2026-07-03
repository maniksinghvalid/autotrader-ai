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
    from autotrader.scheduler import (
        LifecycleScheduler, RISK_SWEEP, REBALANCE, RISK_CHECK_MID, RISK_CHECK_LATE,
    )
    s = LifecycleScheduler()
    s.poll(_dt(10, 0))            # fire the two morning jobs (PRE_OPEN_SYNC, ENTRY_OPEN)
    s.poll(_dt(13, 0))            # fire REBALANCE (12:30 past due)
    s.poll(_dt(14, 0))            # fire RISK_CHECK_MID (13:30 past due)
    s.poll(_dt(15, 0))            # fire RISK_CHECK_LATE (15:00 exactly)
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


def test_eod_report_fires_after_1630_and_once_per_day():
    from autotrader.scheduler import LifecycleScheduler, EOD_REPORT
    s = LifecycleScheduler()
    s.poll(_dt(16, 20))                     # nothing new at 16:20 beyond catch-up
    due = s.poll(_dt(16, 31))
    assert EOD_REPORT in due
    assert s.poll(_dt(16, 45)) == []        # does not re-fire same day


def test_eod_report_fires_after_eod_flatten():
    from autotrader.scheduler import LifecycleScheduler, EOD_FLATTEN, EOD_REPORT
    s = LifecycleScheduler()
    due = s.poll(_dt(16, 31))               # first poll catches up both, in order
    assert due.index(EOD_FLATTEN) < due.index(EOD_REPORT)


def test_at_most_once_jobs_survive_restart():
    """Restart after 16:30 must NOT re-post the EOD Slack report or re-run
    REBALANCE (review Minor #8) — their last-fired dates persist."""
    from datetime import datetime
    from zoneinfo import ZoneInfo
    from autotrader.scheduler import (LifecycleScheduler, EOD_REPORT, REBALANCE,
                                      RISK_CHECK_LATE)
    _NY = ZoneInfo("America/New_York")
    store = {}
    s1 = LifecycleScheduler(state_get=store.get, state_set=store.__setitem__)
    fired = s1.poll(datetime(2026, 7, 6, 16, 31, tzinfo=_NY))
    assert EOD_REPORT in fired and REBALANCE in fired

    # "restart": a NEW scheduler over the same persisted state
    s2 = LifecycleScheduler(state_get=store.get, state_set=store.__setitem__)
    refired = s2.poll(datetime(2026, 7, 6, 16, 35, tzinfo=_NY))
    assert EOD_REPORT not in refired and REBALANCE not in refired
    # catch-up of NON-at-most-once jobs is deliberately preserved (halt recovery)
    assert RISK_CHECK_LATE in refired


def test_at_most_once_fires_fresh_next_day():
    from datetime import datetime
    from zoneinfo import ZoneInfo
    from autotrader.scheduler import LifecycleScheduler, EOD_REPORT
    _NY = ZoneInfo("America/New_York")
    store = {}
    s1 = LifecycleScheduler(state_get=store.get, state_set=store.__setitem__)
    s1.poll(datetime(2026, 7, 6, 16, 31, tzinfo=_NY))
    s2 = LifecycleScheduler(state_get=store.get, state_set=store.__setitem__)
    assert EOD_REPORT in s2.poll(datetime(2026, 7, 7, 16, 31, tzinfo=_NY))
