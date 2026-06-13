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
EOD_FLATTEN = "EOD_FLATTEN"       # 16:15 — cancel-all + commit + halt for the day

# Chronological order is load-bearing: poll() returns due jobs in this order.
_SCHEDULE: Tuple[Tuple[str, time], ...] = (
    (PRE_OPEN_SYNC, time(8, 30)),
    (ENTRY_OPEN, time(9, 45)),
    (RISK_SWEEP, time(15, 30)),
    (EOD_FLATTEN, time(16, 15)),
)


class LifecycleScheduler:
    def __init__(self) -> None:
        self._last_fired: Dict[str, str] = {}  # job name -> ISO date it last fired

    def poll(self, now: datetime) -> List[str]:
        today = now.date().isoformat()
        due: List[str] = []
        for name, sched in _SCHEDULE:
            if now.time() >= sched and self._last_fired.get(name) != today:
                self._last_fired[name] = today
                due.append(name)
        return due
