"""Deterministic EST lifecycle scheduler — Gemini's four daily crons, made
testable. poll(now) returns the job names that have become due since the last
poll, in chronological order, firing each at most once per calendar day. A
late start catches up all past-due jobs on the first poll (so the pre-open
sync still runs if the process started after 08:30)."""
from __future__ import annotations

from datetime import datetime, time
from typing import Dict, List, Tuple

PRE_OPEN_SYNC = "PRE_OPEN_SYNC"   # 08:30 — broker ground-truth sync
ENTRY_OPEN = "ENTRY_OPEN"         # 09:45 — entry window opens
RISK_SWEEP = "RISK_SWEEP"         # 15:30 — entries close, reconcile + record perf
EOD_CANCEL_ORDERS = "EOD_FLATTEN"   # 16:15 — cancel-all working orders + commit perf.
                                    # Keeps the legacy string value so persisted
                                    # scheduler state and old logs stay readable.
EOD_FLATTEN = EOD_CANCEL_ORDERS     # deprecated alias — positions are NOT flattened
REBALANCE = "REBALANCE"           # 12:30 — drift-band rebalance + stop consolidation
RISK_CHECK_MID = "RISK_CHECK_MID"   # 13:30 — tiered intraday risk re-check
RISK_CHECK_LATE = "RISK_CHECK_LATE"  # 15:00 — tiered intraday risk re-check
EOD_REPORT = "EOD_REPORT"         # 16:30 — post session summary to Slack

# Chronological order is load-bearing: poll() returns due jobs in this order.
_SCHEDULE: Tuple[Tuple[str, time], ...] = (
    (PRE_OPEN_SYNC, time(8, 30)),
    (ENTRY_OPEN, time(9, 45)),
    (REBALANCE, time(12, 30)),
    (RISK_CHECK_MID, time(13, 30)),
    (RISK_CHECK_LATE, time(15, 0)),
    (RISK_SWEEP, time(15, 30)),
    (EOD_CANCEL_ORDERS, time(16, 15)),
    (EOD_REPORT, time(16, 30)),
)


# Jobs that must fire at most once per day even across a process restart —
# a restart after 16:30 must not re-post the EOD Slack report or re-run the
# rebalance. Everything else (syncs, gate open/close, risk checks) DELIBERATELY
# re-fires on catch-up: that is the halt-recovery behavior after a mid-day
# restart (do not "fix" it by adding jobs here casually).
AT_MOST_ONCE = frozenset({REBALANCE, EOD_CANCEL_ORDERS, EOD_REPORT})


class LifecycleScheduler:
    def __init__(self, state_get=None, state_set=None) -> None:
        self._last_fired: Dict[str, str] = {}  # job name -> ISO date it last fired
        # Optional durable backing (DB.get_state/set_state) for AT_MOST_ONCE jobs.
        self._state_get = state_get
        self._state_set = state_set

    def _last(self, name: str):
        if name in self._last_fired:
            return self._last_fired[name]
        if name in AT_MOST_ONCE and self._state_get is not None:
            return self._state_get(f"sched:{name}")
        return None

    def poll(self, now: datetime) -> List[str]:
        today = now.date().isoformat()
        due: List[str] = []
        for name, sched in _SCHEDULE:
            if now.time() >= sched and self._last(name) != today:
                self._last_fired[name] = today
                if name in AT_MOST_ONCE and self._state_set is not None:
                    self._state_set(f"sched:{name}", today)
                due.append(name)
        return due
