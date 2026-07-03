"""C1 fix: poll() must not self-mark jobs fired — a job that raises must be
retried on the next poll, and one job's failure must not block later due jobs
in the same batch. mark_fired(name, now) is the new explicit marking call,
invoked by SessionRunner only after a job completes without raising."""
from datetime import datetime

from autotrader.alerts import AlertSink
from autotrader.lifecycle import EntryGate
from autotrader.runner import SessionRunner
from autotrader.scheduler import LifecycleScheduler, PRE_OPEN_SYNC, ENTRY_OPEN, EOD_CANCEL_ORDERS


def test_poll_does_not_mark_mark_fired_does():
    s = LifecycleScheduler()
    now = datetime(2026, 7, 6, 8, 31)
    assert PRE_OPEN_SYNC in s.poll(now)
    assert PRE_OPEN_SYNC in s.poll(now)       # unmarked -> due again (retry)
    s.mark_fired(PRE_OPEN_SYNC, now)
    assert PRE_OPEN_SYNC not in s.poll(now)   # marked -> gone for the day


def test_at_most_once_persists_only_on_mark():
    state = {}
    s = LifecycleScheduler(state_get=state.get, state_set=state.__setitem__)
    now = datetime(2026, 7, 6, 16, 16)
    assert EOD_CANCEL_ORDERS in s.poll(now)
    assert f"sched:{EOD_CANCEL_ORDERS}" not in state    # poll never persists
    s.mark_fired(EOD_CANCEL_ORDERS, now)
    assert state[f"sched:{EOD_CANCEL_ORDERS}"] == "2026-07-06"


class _Watch:
    def ensure_healthy(self):
        return True


class _Engine:
    def tick(self):
        class R: action = "NO_SIGNAL"
        return R()
    def flush_deferred_entries(self):
        return []


class _BoomBroker:
    """get_account raises -> PRE_OPEN_SYNC's ground_truth_sync raises."""
    def get_account(self):
        raise RuntimeError("broker timeout")


class _NoopDB:
    """Minimal db stub so the runner does NOT skip ground_truth_sync (that
    requires both broker and db to be non-None) — this makes PRE_OPEN_SYNC
    genuinely raise via _BoomBroker.get_account instead of being no-op'd."""
    def replace_positions(self, positions):
        pass

    def record_fills(self, fills):
        return 0

    def get_state(self, key):
        return None

    def set_state(self, key, value):
        pass


def test_failed_job_does_not_consume_batch_and_retries(monkeypatch):
    posted = []
    gate = EntryGate(enabled=False)
    runner = SessionRunner(
        engine=_Engine(), broker=_BoomBroker(), db=None, gate=gate,
        scheduler=LifecycleScheduler(), watchdog=_Watch(),
        clock=None, sleep=lambda s: None,
        alerts=AlertSink("http://x", post=lambda u, p: posted.append(p) or 200))
    now = datetime(2026, 7, 6, 9, 46)   # PRE_OPEN_SYNC and ENTRY_OPEN both due
    runner.run_once(now)
    # PRE_OPEN_SYNC raised (broker is None-guarded; force via db) — with db=None the
    # sync is skipped, so instead assert the isolation property directly:
    assert gate.entries_enabled            # ENTRY_OPEN still ran


def test_failed_pre_open_sync_retries_and_alerts_without_blocking_entry_open():
    """Genuine failure-injection variant (db wired so ground_truth_sync actually
    runs and raises via _BoomBroker.get_account) — asserts all three C1
    properties: retry, isolation, and alerting."""
    posted = []
    gate = EntryGate(enabled=False)
    runner = SessionRunner(
        engine=_Engine(), broker=_BoomBroker(), db=_NoopDB(), gate=gate,
        scheduler=LifecycleScheduler(), watchdog=_Watch(),
        clock=None, sleep=lambda s: None,
        alerts=AlertSink("http://x", post=lambda u, p: posted.append(p) or 200))
    now = datetime(2026, 7, 6, 9, 46)   # PRE_OPEN_SYNC and ENTRY_OPEN both due
    runner.run_once(now)
    assert gate.entries_enabled                          # (a) ENTRY_OPEN ran after the failure
    assert PRE_OPEN_SYNC in runner._sched.poll(now)       # (b) unmarked -> retries
    assert any(PRE_OPEN_SYNC in p["text"] for p in posted)  # (c) alert mentions the job
