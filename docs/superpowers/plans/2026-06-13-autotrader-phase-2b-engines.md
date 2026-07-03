# AutoTrader Phase 2b — Engines (Lifecycle Scheduler · Watchdog · Continuous Session Runner)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Turn the deterministic single-tick core into a continuous, self-healing paper-trading session: an injectable wall-clock, an EST lifecycle scheduler (the four Gemini crons — 08:30/09:45/15:30/16:15), a broker→SQLite ground-truth sync, an exponential-backoff watchdog, an entry-window gate, and a `SessionRunner` that ties them together — all offline-testable with `SimBroker` and injected fakes, no real sleeping, no OpenD.

**Architecture:** Five new pure/injectable modules sit *above* the unchanged deterministic core (strategy → `risk_core.evaluate()` → `OrderRouter`). `clock.py` isolates time so the scheduler and watchdog are deterministic under test. `scheduler.py` fires each daily job once, in chronological order, with catch-up. `lifecycle.py` holds the job actions (ground-truth sync into the 2a SQLite projection) and the `EntryGate`. `watchdog.py` is a heartbeat + capped exponential backoff that runs a full reconcile before resuming (research E8). `runner.py`'s `SessionRunner.run_once(now)` executes one loop iteration — tests drive it with fixed times; `run()` loops with an injected clock + sleep + stop predicate. None of the new modules import the SDK; the engines layer is added to the SDK-confinement guard.

**Tech Stack:** Python 3.11 · `zoneinfo` + `tzdata` (stdlib + tiny data pkg) · sqlite3/threading (stdlib) · pytest ≥ 8.0

---

## Phase 2 Scope Note

This is the second of three Phase 2 execution plans (see the 2a scope note and roadmap §4):
- **Plan 2a — Foundation (DONE):** carry-over fixes + rate limiter + SQLite WAL projection.
- **Plan 2b — Engines (this plan):** continuous subscribe-driven loop + EST lifecycle scheduler + watchdog + ground-truth sync + entry gate.
- **Plan 2c — Signals (next):** Pydantic external-signal ingress (localhost-only) + broker-resting `TRAILING_STOP` stops.

**Deliberately deferred to 2c / later:** Pydantic ingress, broker-resting trailing stops, dashboard read-wiring of the SQLite projection, ex-dividend logic (Phase 4). 2b does NOT change the risk core or the order path.

---

## Acceptance Criteria

The plan is **done** only when every behavioral criterion below holds AND its verification command is green. Each behavioral criterion (BC) states an observable guarantee; the verification commands (VC) mechanically prove it. The behavioral criteria are derived from the roadmap §4 Phase-2 capability list; the live "multi-week clean paper session" exit gate is human-verified and stated separately at the end (out of automated scope for 2b).

### Behavioral acceptance criteria (observable guarantees)

**BC-1 — The four daily jobs fire once per day, in order, and catch up after a late start.**
Given a fresh scheduler, when `poll()` is first called at 10:00 ET, then `PRE_OPEN_SYNC` then `ENTRY_OPEN` fire (chronological), neither re-fires later that day, and a new calendar day re-arms all jobs.
*Proven by:* VC-2.

**BC-2 — The pre-open sync makes the SQLite projection match broker ground truth, idempotently.**
Given broker positions and fills, when `PRE_OPEN_SYNC` runs, then every position is upserted into `positions` and every fill recorded in `fills`; running it again records **zero** new fills (dedupe by `fill_id`). The broker is the source of truth; the projection is overwritten to match.
*Proven by:* VC-3, VC-6 (`test_run_once_pre_open_sync_writes_positions`).

**BC-3 — The entry window gates NEW entries only; exits are never blocked.**
Given entries closed (before 09:45 ET), when a BUY signal is produced, then no order is placed and the tick reports `ENTRY_CLOSED`; a SELL (exit) signal still routes to the risk core and places. After `ENTRY_OPEN` (09:45) fires, BUY signals place.
*Proven by:* VC-5, VC-6 (`test_run_once_entry_open_enables_then_buy_places`).

**BC-4 — The 15:30 sweep closes the entry window and records a performance snapshot.**
Given an open entry window, when `RISK_SWEEP` runs, then `entries_enabled` becomes False and a `performance` row exists for the day.
*Proven by:* VC-6 (`test_run_once_risk_sweep_closes_entries_and_records_perf`).

**BC-5 — The 16:15 flatten cancels every working order and commits performance.**
Given a working (unfilled) order, when `EOD_FLATTEN` runs, then `get_open_orders()` is empty, `entries_enabled` is False, and a performance row is committed.
*Proven by:* VC-6 (`test_run_once_eod_flatten_cancels_all`).

**BC-6 — The watchdog never resumes trading on a dead connection.**
Given a failing heartbeat, when an iteration runs, then it returns `HALTED_UNHEALTHY` and **no** engine tick executes; backoff is capped-exponential; and on recovery a full reconcile runs **before** the loop resumes (research E8).
*Proven by:* VC-4, VC-6 (`test_run_once_halts_when_watchdog_unhealthy`).

**BC-7 — The continuous loop runs until told to stop and survives a bad iteration.**
Given a stop predicate, when `run()` executes, then it calls `run_once` each iteration until `stop()` is True, and an exception in one iteration is logged without killing the session.
*Proven by:* VC-6 (`test_run_loops_until_stop`), and the `try/except` in `SessionRunner.run` (Task 6).

**BC-8 — The engines layer adds no new order path and never imports the SDK.**
Given the new modules (`clock`, `scheduler`, `lifecycle`, `watchdog`, `runner`), when they are imported in a fresh process, then `moomoo` is **not** loaded; and every order still flows only through `risk_core.evaluate()` → `OrderRouter` (no module calls `broker.place_order` except the router; lifecycle jobs only `get_account`/`reconcile_fills`/`cancel_all`).
*Proven by:* VC-7, plus the unchanged router/risk-core (no edits in this plan touch the order path).

**BC-9 — Time and sleep are fully injectable; no test depends on the wall clock or actually sleeps.**
Given `FixedClock` and an injected `sleep`, when any time-dependent test runs, then it is deterministic and completes without real delay.
*Proven by:* VC-1, VC-4, VC-6 (all use `FixedClock` / recorder sleeps).

**BC-10 — No regression in the existing core.**
Given the full offline suite, when it runs, then every prior test (Phase 1 + 2a) still passes and nothing is skipped.
*Proven by:* VC-8.

### Verification commands (mechanical proof — all must be green)

| VC | Command | Expected | Proves |
|---|---|---|---|
| VC-1 clock | `pytest tests/test_clock.py` | 3 passed | BC-9 |
| VC-2 scheduler | `pytest tests/test_scheduler.py` | 5 passed | BC-1 |
| VC-3 lifecycle | `pytest tests/test_lifecycle.py` | 4 passed | BC-2 |
| VC-4 watchdog | `pytest tests/test_watchdog.py` | 6 passed | BC-6, BC-9 |
| VC-5 entry gate | `pytest tests/test_main_loop.py -k entry_gate` | 3 passed | BC-3 |
| VC-6 runner | `pytest tests/test_runner.py` | 6 passed | BC-2..BC-7, BC-9 |
| VC-7 SDK confinement | `pytest tests/test_no_sdk_in_core.py` | 1 passed (now imports `clock`, `scheduler`, `lifecycle`, `watchdog`, `runner`) | BC-8 |
| VC-8 full suite | `pytest tests/ --ignore=tests/test_moomoo_broker_live.py` | 97 passed, 0 skipped | BC-10 |

> Count math for VC-8: 70 (after 2a) + 3 clock + 5 scheduler + 4 lifecycle + 6 watchdog + 3 entry-gate + 6 runner = **97**. If your post-2a baseline differs, adjust to baseline + 27 and keep "0 skipped".

### Live exit gate (human-verified — NOT part of automated 2b completion)

Per roadmap §4 Phase 2, before promoting past 2b the system must additionally demonstrate, against a real OpenD paper account: **N clean unattended sessions** (full 08:30→16:15 lifecycle, no unhandled exceptions), **P&L and max-drawdown tracked** in the `performance` table within configured limits, and **zero safety violations** (no order without its audit line; no trade while the watchdog reports unhealthy; entries only inside the 09:45–15:30 window). This gate is signed off by a human reviewer and recorded in the roadmap; it is not asserted by the offline suite.

---

## File Structure

| File | Action | Responsibility |
|---|---|---|
| `pyproject.toml` | **Modify** | Add `tzdata` dependency (guarantees `zoneinfo` tz database cross-platform) |
| `autotrader/clock.py` | **Create** | `Clock.now_est()` (America/New_York) + `FixedClock` for tests |
| `autotrader/scheduler.py` | **Create** | `LifecycleScheduler.poll(now)` → newly-due job names; job-name constants |
| `autotrader/lifecycle.py` | **Create** | `ground_truth_sync(broker, db, since)` → `SyncResult`; `EntryGate` (open/close/`entries_enabled`) |
| `autotrader/watchdog.py` | **Create** | `backoff_seconds(attempt)`; `Watchdog.ensure_healthy()` (heartbeat + backoff + reconcile-before-resume) |
| `autotrader/runner.py` | **Create** | `SessionRunner.run_once(now)` + `run(stop)` — the continuous loop |
| `autotrader/broker.py` | **Modify** | Add `heartbeat()` to the base contract (defaults to `is_ready()`) |
| `autotrader/moomoo_broker.py` | **Modify** | Override `heartbeat()` using `get_global_state` (live-only path) |
| `autotrader/main.py` | **Modify** | Add optional `entry_gate` to `TradeEngine`; gate BUY entries; replace single-tick `main()` body with `SessionRunner.run()` |
| `tests/test_clock.py` | **Create** | 3 tests |
| `tests/test_scheduler.py` | **Create** | 5 tests |
| `tests/test_lifecycle.py` | **Create** | 4 tests |
| `tests/test_watchdog.py` | **Create** | 6 tests |
| `tests/test_runner.py` | **Create** | 6 tests |
| `tests/test_main_loop.py` | **Modify** | Add 3 entry-gate tests |
| `tests/test_no_sdk_in_core.py` | **Modify** | Add the 5 new modules to the import list |

---

### Task 1: `tzdata` dependency + Clock abstraction

**Files:**
- Modify: `pyproject.toml`
- Create: `autotrader/clock.py`
- Create: `tests/test_clock.py`

- [ ] **Step 1: Add `tzdata` to dependencies**

In `pyproject.toml`, change the `dependencies` line:

```toml
dependencies = ["moomoo-api>=10.4.6408", "tzdata>=2024.1"]
```

(`tzdata` is a pure-data package; it guarantees `zoneinfo.ZoneInfo("America/New_York")` resolves even on systems without a system tz database. Install it now: `python3 -m pip install "tzdata>=2024.1"`.)

- [ ] **Step 2: Write the failing tests**

Create `tests/test_clock.py`:

```python
"""Clock tests. FixedClock makes every time-dependent module deterministic."""
from datetime import datetime
from zoneinfo import ZoneInfo


def test_fixed_clock_returns_the_preset_time():
    from autotrader.clock import FixedClock
    dt = datetime(2026, 6, 12, 9, 45, tzinfo=ZoneInfo("America/New_York"))
    c = FixedClock(dt)
    assert c.now_est() == dt


def test_fixed_clock_set_updates_the_time():
    from autotrader.clock import FixedClock
    ny = ZoneInfo("America/New_York")
    c = FixedClock(datetime(2026, 6, 12, 8, 30, tzinfo=ny))
    c.set(datetime(2026, 6, 12, 16, 15, tzinfo=ny))
    assert c.now_est().hour == 16 and c.now_est().minute == 15


def test_real_clock_is_timezone_aware_new_york():
    from autotrader.clock import Clock
    now = Clock().now_est()
    assert now.tzinfo is not None
    assert "New_York" in str(now.tzinfo)
```

- [ ] **Step 3: Run tests to verify they fail**

```bash
cd /Users/acdc/Documents/AI/AutoTrader
python3 -m pytest tests/test_clock.py -v
```

Expected: FAIL with `ModuleNotFoundError: No module named 'autotrader.clock'`

- [ ] **Step 4: Create `autotrader/clock.py`**

```python
"""Wall-clock abstraction in US market time (America/New_York — handles the
EST/EDT transition automatically, unlike a fixed-offset 'EST'). Injecting a
Clock lets the scheduler and watchdog be tested deterministically: production
uses Clock(); tests use FixedClock."""
from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

_NY = ZoneInfo("America/New_York")


class Clock:
    """Real wall-clock in market time."""

    def now_est(self) -> datetime:
        return datetime.now(_NY)


class FixedClock(Clock):
    """Deterministic clock for tests. Holds a single tz-aware datetime."""

    def __init__(self, dt: datetime):
        self._dt = dt

    def now_est(self) -> datetime:
        return self._dt

    def set(self, dt: datetime) -> None:
        self._dt = dt
```

- [ ] **Step 5: Run tests to verify they pass**

```bash
python3 -m pytest tests/test_clock.py -v
```

Expected: 3 passed.

- [ ] **Step 6: Commit**

```bash
git add pyproject.toml autotrader/clock.py tests/test_clock.py
git commit -m "feat: add Clock abstraction (America/New_York) + tzdata dep"
```

---

### Task 2: Lifecycle scheduler

**Files:**
- Create: `autotrader/scheduler.py`
- Create: `tests/test_scheduler.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_scheduler.py`:

```python
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
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
python3 -m pytest tests/test_scheduler.py -v
```

Expected: FAIL with `ModuleNotFoundError: No module named 'autotrader.scheduler'`

- [ ] **Step 3: Create `autotrader/scheduler.py`**

```python
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
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
python3 -m pytest tests/test_scheduler.py -v
```

Expected: 5 passed.

- [ ] **Step 5: Commit**

```bash
git add autotrader/scheduler.py tests/test_scheduler.py
git commit -m "feat: deterministic EST lifecycle scheduler (4 daily jobs, catch-up)"
```

---

### Task 3: Ground-truth sync + entry gate

**Files:**
- Create: `autotrader/lifecycle.py`
- Create: `tests/test_lifecycle.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_lifecycle.py`:

```python
"""Lifecycle job actions: ground_truth_sync writes broker truth into the 2a
SQLite projection; EntryGate tracks whether new entries are permitted."""
from autotrader.db import DB
from autotrader.sim_broker import SimBroker
from autotrader.domain import OrderRequest


def test_ground_truth_sync_upserts_positions_and_records_fills(tmp_path):
    from autotrader.lifecycle import ground_truth_sync
    db = DB(str(tmp_path / "gt.db"))
    b = SimBroker(quotes={"US.AAPL": 150.0}, cash=100000.0)
    # one filled buy -> SimBroker now holds a position and has a fill
    b.place_order(OrderRequest(symbol="US.AAPL", side="BUY", qty=10,
                               order_type="LIMIT", limit_price=150.0,
                               client_order_id="seed"))
    res = ground_truth_sync(b, db, since=None)
    assert res.positions == 1
    assert res.new_fills == 1
    pos = db._conn.execute("SELECT qty FROM positions WHERE symbol='US.AAPL'").fetchone()
    assert pos[0] == 10
    db.close()


def test_ground_truth_sync_is_idempotent_on_fills(tmp_path):
    from autotrader.lifecycle import ground_truth_sync
    db = DB(str(tmp_path / "gt.db"))
    b = SimBroker(quotes={"US.AAPL": 150.0}, cash=100000.0)
    b.place_order(OrderRequest(symbol="US.AAPL", side="BUY", qty=5,
                               order_type="LIMIT", limit_price=150.0,
                               client_order_id="seed"))
    assert ground_truth_sync(b, db, since=None).new_fills == 1
    assert ground_truth_sync(b, db, since=None).new_fills == 0, "duplicate fills dedupe"
    db.close()


def test_entry_gate_defaults_closed_and_opens():
    from autotrader.lifecycle import EntryGate
    g = EntryGate()
    assert g.entries_enabled is False
    g.open()
    assert g.entries_enabled is True


def test_entry_gate_close_disables():
    from autotrader.lifecycle import EntryGate
    g = EntryGate(enabled=True)
    g.close()
    assert g.entries_enabled is False
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
python3 -m pytest tests/test_lifecycle.py -v
```

Expected: FAIL with `ModuleNotFoundError: No module named 'autotrader.lifecycle'`

- [ ] **Step 3: Create `autotrader/lifecycle.py`**

```python
"""Lifecycle job actions and the entry-window gate.

ground_truth_sync pulls broker truth (positions + fills) into the SQLite
projection from Plan 2a — the broker is the source of truth, the projection a
read-through cache (research: overwrite local drift with broker state). Fills
dedupe by fill_id inside DB.record_fills, so re-running is safe.

EntryGate gates *new* entries (BUY). Exits (SELL) are never gated — you must
always be able to flatten a position."""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

from autotrader.broker import Broker
from autotrader.db import DB

logger = logging.getLogger("autotrader.lifecycle")


@dataclass(frozen=True)
class SyncResult:
    positions: int
    new_fills: int


def ground_truth_sync(broker: Broker, db: DB, since: Optional[str] = None) -> SyncResult:
    snap = broker.get_account()
    db.upsert_positions(list(snap.positions))
    fills = broker.reconcile_fills(since)
    new = db.record_fills(fills)
    logger.info("ground_truth_sync: %d position(s), %d new fill(s)",
                len(snap.positions), new)
    return SyncResult(positions=len(snap.positions), new_fills=new)


class EntryGate:
    """Whether new BUY entries are currently permitted. Defaults closed."""

    def __init__(self, enabled: bool = False):
        self._enabled = enabled

    @property
    def entries_enabled(self) -> bool:
        return self._enabled

    def open(self) -> None:
        self._enabled = True

    def close(self) -> None:
        self._enabled = False
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
python3 -m pytest tests/test_lifecycle.py -v
```

Expected: 4 passed.

- [ ] **Step 5: Commit**

```bash
git add autotrader/lifecycle.py tests/test_lifecycle.py
git commit -m "feat: ground_truth_sync (broker->SQLite) + EntryGate"
```

---

### Task 4: Watchdog (heartbeat + exponential backoff + reconcile-before-resume)

**Files:**
- Create: `autotrader/watchdog.py`
- Create: `tests/test_watchdog.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_watchdog.py`:

```python
"""Watchdog: heartbeat + capped exponential backoff. On recovery it runs a
full reconcile BEFORE signalling the loop may resume (research E8). All sleeps
are injected so tests never actually wait."""


def test_backoff_seconds_is_capped_exponential():
    from autotrader.watchdog import backoff_seconds
    assert backoff_seconds(1) == 1.0
    assert backoff_seconds(2) == 2.0
    assert backoff_seconds(3) == 4.0
    assert backoff_seconds(4) == 8.0
    assert backoff_seconds(5) == 16.0
    assert backoff_seconds(6) == 30.0   # capped
    assert backoff_seconds(7) == 30.0   # stays capped


def test_backoff_seconds_floors_attempt_at_one():
    from autotrader.watchdog import backoff_seconds
    assert backoff_seconds(0) == 1.0


def test_ensure_healthy_true_when_first_heartbeat_ok():
    from autotrader.watchdog import Watchdog
    slept, reconciled = [], []
    w = Watchdog(health_check=lambda: True,
                 reconcile=lambda: reconciled.append(1),
                 sleep=lambda s: slept.append(s))
    assert w.ensure_healthy() is True
    assert slept == [], "no backoff when healthy"
    assert reconciled == [], "no reconcile when never lost"


def test_ensure_healthy_recovers_and_reconciles_before_resume():
    from autotrader.watchdog import Watchdog
    # unhealthy, unhealthy, then healthy
    seq = iter([False, False, True])
    slept, reconciled = [], []
    w = Watchdog(health_check=lambda: next(seq),
                 reconcile=lambda: reconciled.append(1),
                 sleep=lambda s: slept.append(s))
    assert w.ensure_healthy() is True
    assert slept == [1.0, 2.0], "backed off twice before recovery"
    assert reconciled == [1], "reconcile ran exactly once, on recovery"


def test_ensure_healthy_false_after_max_attempts_no_reconcile():
    from autotrader.watchdog import Watchdog
    reconciled = []
    w = Watchdog(health_check=lambda: False,
                 reconcile=lambda: reconciled.append(1),
                 sleep=lambda s: None,
                 max_attempts=3)
    assert w.ensure_healthy() is False
    assert reconciled == [], "never reconcile if never recovered"


def test_ensure_healthy_respects_max_attempts_count():
    from autotrader.watchdog import Watchdog
    calls = {"n": 0}

    def health():
        calls["n"] += 1
        return False

    w = Watchdog(health_check=health, reconcile=lambda: None,
                 sleep=lambda s: None, max_attempts=4)
    w.ensure_healthy()
    # 1 initial check + 4 retry checks = 5 calls
    assert calls["n"] == 5
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
python3 -m pytest tests/test_watchdog.py -v
```

Expected: FAIL with `ModuleNotFoundError: No module named 'autotrader.watchdog'`

- [ ] **Step 3: Create `autotrader/watchdog.py`**

```python
"""Connection watchdog: heartbeat + capped exponential-backoff reconnect.

CLAUDE.md forbids time.sleep() as a *readiness check* — this is exponential
backoff polling, and the sleep function is injected (production passes
time.sleep; tests pass a recorder). On recovery the watchdog runs a full
reconcile BEFORE returning True, so the loop never resumes on stale state
(research E8)."""
from __future__ import annotations

import logging
from typing import Callable

logger = logging.getLogger("autotrader.watchdog")


def backoff_seconds(attempt: int, base: float = 1.0, cap: float = 30.0) -> float:
    """1-based exponential backoff, capped. attempt<=1 -> base."""
    if attempt < 1:
        attempt = 1
    return min(cap, base * (2 ** (attempt - 1)))


class Watchdog:
    def __init__(self, health_check: Callable[[], bool],
                 reconcile: Callable[[], None],
                 sleep: Callable[[float], None],
                 max_attempts: int = 6):
        self._health = health_check
        self._reconcile = reconcile
        self._sleep = sleep
        self._max_attempts = max_attempts

    def ensure_healthy(self) -> bool:
        """True if healthy now, or recovered within max_attempts (a full
        reconcile is run on recovery). False if still unhealthy."""
        if self._health():
            return True
        for attempt in range(1, self._max_attempts + 1):
            delay = backoff_seconds(attempt)
            logger.warning("watchdog: unhealthy — backoff %.1fs (attempt %d/%d)",
                           delay, attempt, self._max_attempts)
            self._sleep(delay)
            if self._health():
                logger.info("watchdog: recovered — full reconcile before resume")
                self._reconcile()
                return True
        logger.error("watchdog: still unhealthy after %d attempts", self._max_attempts)
        return False
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
python3 -m pytest tests/test_watchdog.py -v
```

Expected: 6 passed.

- [ ] **Step 5: Commit**

```bash
git add autotrader/watchdog.py tests/test_watchdog.py
git commit -m "feat: connection watchdog (heartbeat + capped backoff + reconcile-on-resume)"
```

---

### Task 5: Broker.heartbeat + entry-gate wiring in TradeEngine

**Files:**
- Modify: `autotrader/broker.py`
- Modify: `autotrader/moomoo_broker.py`
- Modify: `autotrader/main.py` (`TradeEngine`)
- Modify: `tests/test_main_loop.py`

- [ ] **Step 1: Write the failing tests**

Add at the bottom of `tests/test_main_loop.py`:

```python
def test_entry_gate_blocks_buy_when_closed(tmp_path):
    from autotrader.lifecycle import EntryGate
    b = SimBroker(quotes={"US.AAPL": 101.0}, cash=100000.0)
    strat = ThresholdStrategy(StrategyParams(symbol="US.AAPL", entry_price=100.0,
                                             stop_loss_pct=0.05, take_profit_pct=0.10,
                                             confidence=0.7))
    eng = TradeEngine(broker=b, strategy=strat, cfg=_cfg(), order_qty=10,
                      audit_path=str(tmp_path / "audit.jsonl"),
                      entry_gate=EntryGate(enabled=False))
    result = eng.tick()
    assert result.action == "ENTRY_CLOSED"
    assert b.get_account().position_qty("US.AAPL") == 0


def test_entry_gate_allows_buy_when_open(tmp_path):
    from autotrader.lifecycle import EntryGate
    b = SimBroker(quotes={"US.AAPL": 101.0}, cash=100000.0)
    strat = ThresholdStrategy(StrategyParams(symbol="US.AAPL", entry_price=100.0,
                                             stop_loss_pct=0.05, take_profit_pct=0.10,
                                             confidence=0.7))
    eng = TradeEngine(broker=b, strategy=strat, cfg=_cfg(), order_qty=10,
                      audit_path=str(tmp_path / "audit.jsonl"),
                      entry_gate=EntryGate(enabled=True))
    assert eng.tick().action == "ORDER_PLACED"


def test_entry_gate_allows_sell_exit_even_when_closed(tmp_path):
    from autotrader.lifecycle import EntryGate
    from autotrader.domain import OrderRequest
    b = SimBroker(quotes={"US.AAPL": 94.0}, cash=100000.0)
    b.place_order(OrderRequest(symbol="US.AAPL", side="BUY", qty=10,
                               order_type="LIMIT", limit_price=120.0,
                               client_order_id="setup-cid"))
    strat = ThresholdStrategy(StrategyParams(symbol="US.AAPL", entry_price=999.0,
                                             stop_loss_pct=0.05, take_profit_pct=0.10,
                                             confidence=0.7))
    eng = TradeEngine(broker=b, strategy=strat, cfg=_cfg(), order_qty=1,
                      audit_path=str(tmp_path / "audit.jsonl"),
                      entry_gate=EntryGate(enabled=False))  # entries CLOSED
    # quote 94 < stop 114 -> SELL exit; gate must NOT block exits
    assert eng.tick().action == "ORDER_PLACED"
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
python3 -m pytest tests/test_main_loop.py -k entry_gate -v
```

Expected: FAIL with `TypeError: TradeEngine.__init__() got an unexpected keyword argument 'entry_gate'`

- [ ] **Step 3: Add `heartbeat()` to the Broker base contract**

In `autotrader/broker.py`, add this method to the `Broker` class (after `is_ready`):

```python
    def heartbeat(self) -> bool:
        """Liveness probe for the watchdog. Defaults to is_ready(); MoomooBroker
        overrides with an active OpenD query."""
        return self.is_ready()
```

- [ ] **Step 4: Override `heartbeat()` in MoomooBroker**

In `autotrader/moomoo_broker.py`, add this method to `MoomooBroker` (e.g. right after `is_ready`):

```python
    def heartbeat(self) -> bool:  # pragma: no cover — live OpenD path
        if self._quote is None:
            return False
        try:
            ret, _ = self._quote.get_global_state()
        except Exception:
            return False
        return self._ok(ret)
```

- [ ] **Step 5: Add `entry_gate` to TradeEngine and gate BUY entries**

In `autotrader/main.py`, update the imports block to also import the type for the gate (TYPE_CHECKING already added in 2a — extend it):

```python
if TYPE_CHECKING:
    from autotrader.db import DB
    from autotrader.lifecycle import EntryGate
```

Replace `TradeEngine.__init__`:

```python
    def __init__(self, broker: Broker, strategy: ThresholdStrategy, cfg: RiskConfig,
                 order_qty: int, audit_path: str, db: "Optional[DB]" = None,
                 entry_gate: "Optional[EntryGate]" = None):
        self._b = broker
        self._strat = strategy
        self._cfg = cfg
        self._qty = order_qty
        self._router = OrderRouter(broker, audit_path=audit_path)
        self._signal_seq = 0
        self._db = db
        self._gate = entry_gate
```

In `tick()`, insert the entry-gate check immediately after the confidence filter and BEFORE the `if self._db: self._db.record_performance(...)` block:

```python
        # Entry-window gate: block NEW entries (BUY) when closed; exits (SELL)
        # are never gated — you must always be able to flatten.
        if (signal.direction == "BUY" and self._gate is not None
                and not self._gate.entries_enabled):
            return TickResult("ENTRY_CLOSED", signal.symbol)
```

For reference, the surrounding region of `tick()` must read:

```python
        if signal.confidence < self._cfg.min_confidence:
            return TickResult("DROPPED_LOW_CONFIDENCE", f"{signal.confidence}")

        if (signal.direction == "BUY" and self._gate is not None
                and not self._gate.entries_enabled):
            return TickResult("ENTRY_CLOSED", signal.symbol)

        if self._db:
            self._db.record_performance(
                day_pnl=snap.day_pnl,
                total_assets=snap.total_assets,
                cash=snap.cash,
                gross_exposure=snap.gross_exposure(),
            )
```

- [ ] **Step 6: Run the entry-gate tests, then the full main-loop file**

```bash
python3 -m pytest tests/test_main_loop.py -k entry_gate -v
python3 -m pytest tests/test_main_loop.py -v
```

Expected: 3 passed (entry-gate), then 11 passed (8 from 2a + 3 new).

- [ ] **Step 7: Commit**

```bash
git add autotrader/broker.py autotrader/moomoo_broker.py autotrader/main.py tests/test_main_loop.py
git commit -m "feat: Broker.heartbeat + EntryGate-gated BUY entries (SELL exits always allowed)"
```

---

### Task 6: SessionRunner — the continuous loop

**Files:**
- Create: `autotrader/runner.py`
- Create: `tests/test_runner.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_runner.py`:

```python
"""SessionRunner ties scheduler + lifecycle jobs + watchdog + engine into one
loop. run_once(now) executes a single iteration; tests drive it with a
FixedClock and assert job side-effects, DB writes, and tick outcomes. No real
sleeping or OpenD."""
from datetime import datetime
from zoneinfo import ZoneInfo

from autotrader.clock import FixedClock
from autotrader.config import RiskConfig
from autotrader.db import DB
from autotrader.lifecycle import EntryGate
from autotrader.scheduler import LifecycleScheduler
from autotrader.sim_broker import SimBroker
from autotrader.strategies.threshold import ThresholdStrategy, StrategyParams
from autotrader.watchdog import Watchdog
from autotrader.main import TradeEngine
from autotrader.runner import SessionRunner
from autotrader.domain import OrderRequest

_NY = ZoneInfo("America/New_York")


def _cfg(**over):
    base = dict(trading_env="PAPER", min_confidence=0.6, max_order_notional=20000,
                max_position_qty=100, daily_loss_limit=500, max_gross_exposure=100000,
                allowed_symbols=frozenset({"US.AAPL"}))
    base.update(over)
    return RiskConfig(**base)


def _dt(h, m, day=12):
    return datetime(2026, 6, day, h, m, tzinfo=_NY)


def _build(tmp_path, broker, *, healthy=True, gate_enabled=False):
    db = DB(str(tmp_path / "runner.db"))
    gate = EntryGate(enabled=gate_enabled)
    strat = ThresholdStrategy(StrategyParams(symbol="US.AAPL", entry_price=100.0,
                                             stop_loss_pct=0.05, take_profit_pct=0.10,
                                             confidence=0.7))
    eng = TradeEngine(broker=broker, strategy=strat, cfg=_cfg(), order_qty=10,
                      audit_path=str(tmp_path / "audit.jsonl"), db=db, entry_gate=gate)
    watch = Watchdog(health_check=lambda: healthy, reconcile=lambda: None,
                     sleep=lambda s: None)
    runner = SessionRunner(engine=eng, broker=broker, db=db, gate=gate,
                           scheduler=LifecycleScheduler(), watchdog=watch,
                           clock=FixedClock(_dt(8, 0)), sleep=lambda s: None)
    return runner, db, gate


def test_run_once_pre_open_sync_writes_positions(tmp_path):
    b = SimBroker(quotes={"US.AAPL": 101.0}, cash=100000.0)
    b.place_order(OrderRequest(symbol="US.AAPL", side="BUY", qty=7,
                               order_type="LIMIT", limit_price=150.0,
                               client_order_id="seed"))
    runner, db, gate = _build(tmp_path, b)
    runner.run_once(_dt(8, 31))   # fires PRE_OPEN_SYNC
    pos = db._conn.execute("SELECT qty FROM positions WHERE symbol='US.AAPL'").fetchone()
    assert pos is not None and pos[0] == 7
    db.close()


def test_run_once_entry_open_enables_then_buy_places(tmp_path):
    b = SimBroker(quotes={"US.AAPL": 101.0}, cash=100000.0)
    runner, db, gate = _build(tmp_path, b)
    # Before 09:45 the gate is closed -> a BUY signal is ENTRY_CLOSED
    assert runner.run_once(_dt(8, 31)) == "ENTRY_CLOSED"
    # At/after 09:45 ENTRY_OPEN fires, gate opens, the BUY is placed
    assert runner.run_once(_dt(9, 46)) == "ORDER_PLACED"
    assert gate.entries_enabled is True
    db.close()


def test_run_once_risk_sweep_closes_entries_and_records_perf(tmp_path):
    b = SimBroker(quotes={"US.AAPL": 101.0}, cash=100000.0)
    runner, db, gate = _build(tmp_path, b)
    runner.run_once(_dt(9, 46))           # open entries
    runner.run_once(_dt(15, 31))          # RISK_SWEEP
    assert gate.entries_enabled is False
    perf = db._conn.execute("SELECT COUNT(*) FROM performance").fetchone()[0]
    assert perf >= 1
    db.close()


def test_run_once_eod_flatten_cancels_all(tmp_path):
    b = SimBroker(quotes={"US.AAPL": 101.0}, cash=100000.0, auto_fill=False)
    runner, db, gate = _build(tmp_path, b, gate_enabled=True)
    runner.run_once(_dt(10, 0))           # places a working (unfilled) order
    assert len(b.get_open_orders()) == 1
    runner.run_once(_dt(16, 16))          # EOD_FLATTEN cancels all
    assert b.get_open_orders() == []
    assert gate.entries_enabled is False
    db.close()


def test_run_once_halts_when_watchdog_unhealthy(tmp_path):
    b = SimBroker(quotes={"US.AAPL": 101.0}, cash=100000.0)
    runner, db, gate = _build(tmp_path, b, healthy=False)
    # Past the morning jobs; watchdog can't recover -> loop reports halt, no tick.
    assert runner.run_once(_dt(10, 0)) == "HALTED_UNHEALTHY"
    db.close()


def test_run_loops_until_stop(tmp_path):
    b = SimBroker(quotes={"US.AAPL": 99.0}, cash=100000.0)  # below entry -> NO_SIGNAL
    runner, db, gate = _build(tmp_path, b)
    calls = {"n": 0}

    def stop():
        calls["n"] += 1
        return calls["n"] > 3   # allow 3 iterations then stop

    runner.run(stop=stop)
    assert calls["n"] == 4, "stop polled until it returned True"
    db.close()
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
python3 -m pytest tests/test_runner.py -v
```

Expected: FAIL with `ModuleNotFoundError: No module named 'autotrader.runner'`

- [ ] **Step 3: Create `autotrader/runner.py`**

```python
"""SessionRunner — the continuous, self-healing paper-trading loop.

Each iteration (run_once):
  1. poll the scheduler; run any newly-due lifecycle jobs (sync / open / sweep / flatten)
  2. ensure the broker connection is healthy (watchdog: backoff + reconcile-on-resume)
  3. if healthy, run one deterministic engine tick

run() loops run_once at the injected clock's time, sleeping loop_interval between
iterations, until the injected stop() predicate is True. The clock and sleep are
injected so the whole loop is testable without real time. Nothing here reaches the
broker except through the engine (which routes via risk_core -> OrderRouter) or the
explicit lifecycle jobs (sync / cancel_all)."""
from __future__ import annotations

import logging
from typing import Callable

from autotrader.clock import Clock
from autotrader.lifecycle import EntryGate, ground_truth_sync
from autotrader.scheduler import (
    LifecycleScheduler, PRE_OPEN_SYNC, ENTRY_OPEN, RISK_SWEEP, EOD_FLATTEN,
)

logger = logging.getLogger("autotrader.runner")


class SessionRunner:
    def __init__(self, engine, broker, db, gate: EntryGate,
                 scheduler: LifecycleScheduler, watchdog, clock: Clock,
                 sleep: Callable[[float], None], loop_interval: float = 5.0):
        self._engine = engine
        self._broker = broker
        self._db = db
        self._gate = gate
        self._sched = scheduler
        self._watch = watchdog
        self._clock = clock
        self._sleep = sleep
        self._loop_interval = loop_interval

    def _record_perf(self) -> None:
        snap = self._broker.get_account()
        self._db.record_performance(snap.day_pnl, snap.total_assets,
                                    snap.cash, snap.gross_exposure())

    def _run_job(self, job: str) -> None:
        if job == PRE_OPEN_SYNC:
            ground_truth_sync(self._broker, self._db)
        elif job == ENTRY_OPEN:
            self._gate.open()
            logger.info("ENTRY_OPEN: entries enabled")
        elif job == RISK_SWEEP:
            self._gate.close()
            ground_truth_sync(self._broker, self._db)
            self._record_perf()
            logger.info("RISK_SWEEP: entries closed, ground truth + performance recorded")
        elif job == EOD_FLATTEN:
            self._gate.close()
            self._broker.cancel_all()
            self._record_perf()
            logger.info("EOD_FLATTEN: entries closed, all orders cancelled, performance committed")

    def run_once(self, now) -> str:
        """Execute one loop iteration. Returns the engine tick action, or
        'HALTED_UNHEALTHY' if the watchdog could not restore the connection."""
        for job in self._sched.poll(now):
            self._run_job(job)
        if not self._watch.ensure_healthy():
            return "HALTED_UNHEALTHY"
        return self._engine.tick().action

    def run(self, stop: Callable[[], bool]) -> None:
        logger.info("SessionRunner started (loop_interval=%.1fs)", self._loop_interval)
        while not stop():
            try:
                action = self.run_once(self._clock.now_est())
                logger.debug("run_once -> %s", action)
            except Exception as e:  # never let one bad iteration kill the session
                logger.error("run_once error: %s", e)
            self._sleep(self._loop_interval)
        logger.info("SessionRunner stopped")
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
python3 -m pytest tests/test_runner.py -v
```

Expected: 6 passed.

- [ ] **Step 5: Commit**

```bash
git add autotrader/runner.py tests/test_runner.py
git commit -m "feat: SessionRunner continuous loop (scheduler + watchdog + engine)"
```

---

### Task 7: Wire SessionRunner into main(); extend SDK-confinement guard

**Files:**
- Modify: `autotrader/main.py` (`main()`)
- Modify: `tests/test_no_sdk_in_core.py`
- Modify: `config/risk.config.example`

- [ ] **Step 1: Extend the SDK-confinement guard to the engines layer**

In `tests/test_no_sdk_in_core.py`, replace the module tuple in the `code` string so it also imports the five new modules:

```python
    code = (
        "import importlib, sys\n"
        "for m in ('autotrader.domain', 'autotrader.config', 'autotrader.risk_core',\n"
        "          'autotrader.broker', 'autotrader.sim_broker', 'autotrader.router',\n"
        "          'autotrader.strategies.threshold', 'autotrader.main',\n"
        "          'autotrader.rate_limiter', 'autotrader.db',\n"
        "          'autotrader.clock', 'autotrader.scheduler', 'autotrader.lifecycle',\n"
        "          'autotrader.watchdog', 'autotrader.runner',\n"
        "          'autotrader.moomoo_broker'):\n"
        "    importlib.import_module(m)\n"
        "assert 'moomoo' not in sys.modules, 'core import pulled in the moomoo SDK'\n"
        "print('ok')\n"
    )
```

- [ ] **Step 2: Run the confinement guard to verify the engines layer is SDK-free**

```bash
python3 -m pytest tests/test_no_sdk_in_core.py -v
```

Expected: 1 passed (importing `clock`, `scheduler`, `lifecycle`, `watchdog`, `runner`, `db`, `rate_limiter` does NOT load `moomoo`).

- [ ] **Step 3: Replace the single-tick `main()` body with the continuous loop**

In `autotrader/main.py`, replace the `main()` engine-construction-and-run block. The current 2a body builds `db`, constructs `engine`, runs a single `engine.tick()`, then shuts down. Replace from the `engine = TradeEngine(...)` line through the end of the `finally` block with:

```python
    from autotrader.clock import Clock
    from autotrader.lifecycle import EntryGate, ground_truth_sync
    from autotrader.scheduler import LifecycleScheduler
    from autotrader.watchdog import Watchdog
    from autotrader.runner import SessionRunner

    gate = EntryGate(enabled=False)  # entries open at 09:45 via the scheduler
    engine = TradeEngine(broker, strat, cfg, order_qty=int(os.getenv("ORDER_QTY", "1")),
                         audit_path=audit, db=db, entry_gate=gate)
    watchdog = Watchdog(
        health_check=broker.heartbeat,
        reconcile=lambda: ground_truth_sync(broker, db),
        sleep=time.sleep,
    )
    runner = SessionRunner(
        engine=engine, broker=broker, db=db, gate=gate,
        scheduler=LifecycleScheduler(), watchdog=watchdog, clock=Clock(),
        sleep=time.sleep,
        loop_interval=float(os.getenv("AUTOTRADER_LOOP_INTERVAL", "5")),
    )

    stopped = {"flag": False}

    def _stop() -> bool:
        return stopped["flag"]

    try:
        runner.run(stop=_stop)        # runs until KeyboardInterrupt
    except KeyboardInterrupt:
        logger.info("interrupt received — shutting down")
    finally:
        # Always disconnect, even if cancel-on-shutdown raises — a failed
        # cancel_all must not leak the OpenD connection.
        try:
            engine.shutdown()
        except Exception as e:
            logger.error("shutdown error (working orders may remain): %s", e)
        broker.close()
        db.close()
    return 0
```

Then add `import time` to the top-level imports of `autotrader/main.py` if it is not already imported inside `main()` (the readiness-gate block already imports `os` and `sys` locally; add `import time` alongside them inside `main()`):

```python
def main() -> int:  # pragma: no cover — live entrypoint, covered by manual run
    import os
    import sys
    import time
    from autotrader.strategies.threshold import StrategyParams
```

- [ ] **Step 4: Document the new env var**

In `config/risk.config.example`, add at the bottom:

```bash
# Continuous-loop iteration interval in seconds (Phase 2b). Default: 5
# AUTOTRADER_LOOP_INTERVAL=5
```

- [ ] **Step 5: Verify main.py still imports cleanly and the engine path is unchanged**

```bash
python3 -c "import autotrader.main; print('main imports OK')"
python3 -m pytest tests/test_main_loop.py -v
```

Expected: `main imports OK`, then 11 passed.

- [ ] **Step 6: Run the full offline suite**

```bash
python3 -m pytest tests/ --ignore=tests/test_moomoo_broker_live.py -q
```

Expected: 97 passed, 0 skipped (baseline 70 + 27 new).

- [ ] **Step 7: Commit**

```bash
git add autotrader/main.py tests/test_no_sdk_in_core.py config/risk.config.example
git commit -m "feat: run continuous SessionRunner in main(); extend SDK-confinement guard to engines layer"
```

---

## Self-Review

### 1. Spec coverage

Checking each Phase 2b item from roadmap §4:

| Roadmap 2b item | Covered by task |
|---|---|
| Continuous loop replacing the single tick | Task 6 (`SessionRunner.run`) + Task 7 (`main()`) ✓ |
| EST lifecycle scheduler — 08:30 / 09:45 / 15:30 / 16:15, readiness-gated | Task 2 (scheduler) + Task 6 (`_run_job` wires each) ✓ |
| 08:30 ground-truth sync (positions + `reconcile_fills` → SQLite, dedupe by `fill_id`) | Task 3 (`ground_truth_sync`) + Task 6 (PRE_OPEN_SYNC) ✓ |
| 09:45 entry-window enable | Task 3 (`EntryGate`) + Task 5 (BUY gating) + Task 6 (ENTRY_OPEN) ✓ |
| 15:30 risk sweep | Task 6 (RISK_SWEEP: close entries + sync + record perf) ✓ |
| 16:15 `cancel_all` + state commit + halt | Task 6 (EOD_FLATTEN) ✓ |
| Watchdog — heartbeat, soft-halt on loss, backoff reconnect, full reconcile before resuming (E8) | Task 4 (`Watchdog`) + Task 5 (`heartbeat`) + Task 6 (HALTED_UNHEALTHY) ✓ |
| Readiness-gated jobs / never trade blind | Task 6 (`run_once` ensures watchdog health before tick; `main()` keeps the existing `is_opend_ready()` startup gate from 2a/Phase 1) ✓ |
| Pydantic external-signal ingress | **Deferred to 2c** (stated in scope note) |
| Broker-resting trailing stops | **Deferred to 2c** (stated in scope note) |
| Dashboard reads SQLite | **Deferred** (read-side wiring; projection is now populated by the sync jobs) |

### 2. Placeholder scan

No "TBD", "TODO", "implement later", or "similar to Task N" patterns. Every code step shows complete code; every command step shows the exact command and expected output.

### 3. Type consistency

- `Clock.now_est() -> datetime` (tz-aware NY); `FixedClock` subclasses it — used by `scheduler.poll(now)` and `SessionRunner.run()` ✓
- `LifecycleScheduler.poll(now: datetime) -> List[str]`; job-name constants `PRE_OPEN_SYNC/ENTRY_OPEN/RISK_SWEEP/EOD_FLATTEN` imported identically in `scheduler.py`, `runner.py`, and tests ✓
- `ground_truth_sync(broker, db, since=None) -> SyncResult(positions:int, new_fills:int)`; uses `broker.get_account()`, `broker.reconcile_fills(since)`, `db.upsert_positions(list)`, `db.record_fills(list)` — all existing 2a signatures ✓
- `EntryGate.entries_enabled: bool`, `.open()`, `.close()` — used by `TradeEngine` (Task 5) and `SessionRunner._run_job` (Task 6) ✓
- `Watchdog(health_check: ()->bool, reconcile: ()->None, sleep: (float)->None, max_attempts=6)`; `ensure_healthy() -> bool`; `backoff_seconds(attempt) -> float` ✓
- `SessionRunner(engine, broker, db, gate, scheduler, watchdog, clock, sleep, loop_interval=5.0)`; `run_once(now) -> str`; `run(stop: ()->bool) -> None` ✓
- `Broker.heartbeat() -> bool` added to base (defaults to `is_ready()`), overridden in `MoomooBroker`; `Watchdog` in `main()` wires `health_check=broker.heartbeat` ✓
- `TradeEngine.__init__(..., db=None, entry_gate=None)` — both optional, existing tests unaffected; new `ENTRY_CLOSED` `TickResult.action` ✓

### 4. Determinism / safety invariants preserved

- The risk core and `OrderRouter` are unchanged; both lifecycle jobs and the engine tick still route every order through `risk_core.evaluate()` (no new order path).
- Time enters only through the injected `Clock`; sleeps only through injected `sleep` — tests run with no real waiting and no wall-clock dependence.
- No new module imports `moomoo`; the SDK-confinement guard is extended to prove it (Task 7).
- SELL exits are never gated; cancel-on-shutdown and "broker always closed" from Phase 1/2a are preserved in the new `main()` `finally`.

---

Plan complete and saved to `docs/superpowers/plans/2026-06-13-autotrader-phase-2b-engines.md`.

**Two execution options:**

**1. Subagent-Driven (recommended)** — fresh subagent per task with two-stage spec + quality review after each.

**2. Inline Execution** — execute tasks in this session with checkpoints.

**Which approach?**
