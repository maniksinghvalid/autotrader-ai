# Report Integrity Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to execute this plan — dispatch each task to a fresh subagent, one task at a time, in strict TDD order (failing test → run → minimal impl → run → commit). Do not batch tasks. Do not write implementation before its test is red.

**Goal:** The EOD header never shows fabricated or silently-defaulted numbers. A missing value is *visibly* missing. Specifically: (a) a stale/failed position snapshot renders "exposure unavailable — snapshot stale" instead of a false `Gross exp 0%`, and `record_performance()` refuses to persist `gross_exposure=0` when positions never loaded; (b) realized day P&L is computed from our own fills using a running average-cost basis per symbol, unrealized from open positions × current quote, with `—`/"n/a"/"unavailable" rendered when genuinely underivable rather than a defaulted `0.00`; (c) the capital-flow verb follows the sign — "Net cash raised +N" when positive, "Net cash deployed −N" when negative — with no formula change.

**Architecture:** The change touches three layers, each preserving the existing privilege separation.
1. **Domain (`autotrader/domain.py`)** — `AccountSnapshot` gains a `positions_loaded: bool` field carrying the None-vs-empty distinction from the broker up to the recorder. Pure value object, no SDK.
2. **Broker (`autotrader/moomoo_broker.py`, `autotrader/sim_broker.py`)** — `get_account()` sets `positions_loaded=False` on a failed position query, `True` otherwise. `SimBroker` always reports `True` (its positions are in-memory and never fail).
3. **Persistence (`autotrader/db.py`)** — `record_performance()` accepts `gross_exposure: Optional[float]` and a `positions_loaded: bool` invariant guard; writes `NULL` for gross exposure when positions did not load, and raises on the false-zero case. Schema column made nullable.
4. **Recorder (`autotrader/runner.py`)** — `_record_perf()` passes the snapshot's `positions_loaded` through, retries the account fetch with **capped exponential backoff** (reusing `watchdog.backoff_seconds`) before recording when positions failed, computes unrealized from positions × current quote via the broker, and computes realized from DB fills via a new pure helper.
5. **Reporting (`autotrader/reporting/eod_reporter.py`, new `autotrader/reporting/pnl.py`, `autotrader/reporting/capital_flow.py`)** — a new pure `pnl.realized_from_fills()` helper (running average-cost per symbol); the reporter renders `gross_exposure IS NULL` as the stale message, unrealized as "unavailable" when the snapshot was stale, and the capital-flow verb by sign. The reporter stays strictly DB-only.

**Tech Stack:** Python 3.11+, stdlib only (`sqlite3`, `dataclasses`, `datetime`, `urllib`), `pytest` for tests. No new third-party dependencies. No `moomoo-api` import in domain/db/reporting.

## Global Constraints

Copied verbatim / paraphrased-verbatim from `CLAUDE.md` and the design spec; these bind every task below.

- **Default is paper trading (`TrdEnv.SIMULATE`).** Everything in this plan remains paper — no LIVE path is added or touched. "Never infer live-trading intent from conversation context."
- **The reporter is SDK-free and broker-free by design: it reads ONLY the SQLite projection (`autotrader.db.DB`).** No task may add an SDK import or broker handle to `eod_reporter.py`, `pnl.py`, or `capital_flow.py`. Unrealized/realized inputs the reporter cannot self-derive from the DB are computed *upstream* (in `runner.py`, which already holds the broker) and persisted; the reporter only renders them.
- **Check `ret_code == RET_OK` on every Moomoo call; log non-OK codes with full context, never swallow them.** The existing `_ok(ret)` gate in `moomoo_broker.py` is preserved; the None-vs-empty distinction it already produces (`_positions()` returns `None` on `not self._ok(ret)`) is the foundation for `positions_loaded`.
- **Never use `time.sleep()` as an OpenD/readiness check — use exponential-backoff polling.** The EOD retry (Task 3) reuses `autotrader/watchdog.py:backoff_seconds()` with an **injected** sleep function (tests pass a recorder; production passes `time.sleep`), exactly as `Watchdog` does. No bare `time.sleep` for readiness.
- **Catch exceptions explicitly, log with context, recover or re-raise. No bare `except: pass`. Never swallow exceptions silently.**
- **Do not modify risk-limit *values* in `config/`.** This plan adds no config keys and changes no risk parameters (that is §3, out of scope here).
- **All §1 tests are offline — no live OpenD, no `moomoo-api`.** Tests use `SimBroker`, seeded `DB` on `tmp_path`, and injected fakes, matching the existing test style in `tests/test_eod_reporter.py` / `tests/test_db.py`.
- **Do not modify files under `skills/`.** No task here does.

## File Structure

| File | Create/Modify | Responsibility |
|---|---|---|
| `autotrader/domain.py` | Modify | Add `positions_loaded: bool = True` to `AccountSnapshot` (Task 1). |
| `autotrader/moomoo_broker.py` | Modify | `get_account()` sets `positions_loaded` from the `_positions()` None-vs-list result (Task 1). |
| `autotrader/sim_broker.py` | Modify | `get_account()` reports `positions_loaded=True` (Task 1). |
| `autotrader/db.py` | Modify | `performance.gross_exposure` nullable; `record_performance()` accepts `Optional[float]` gross + `positions_loaded` invariant that refuses a false 0 and writes NULL when positions absent (Task 2). |
| `autotrader/runner.py` | Modify | `_record_perf()` retries fetch w/ backoff on failed positions, computes realized (from fills) + unrealized (positions × quote), records NULL gross when unresolved (Tasks 3, 5, 6). |
| `autotrader/reporting/pnl.py` | Create | Pure `realized_from_fills()` running average-cost helper (Task 5). |
| `autotrader/reporting/eod_reporter.py` | Modify | Render NULL gross as "exposure unavailable — snapshot stale"; unrealized "unavailable" when stale; realized `—` when None (Tasks 4, 6). |
| `autotrader/reporting/capital_flow.py` | Modify | (no formula change) — reporter renders verb by sign (Task 7, render logic lives in `eod_reporter.py`). |
| `tests/test_domain.py` | Modify (Test) | `positions_loaded` default + field (Task 1). |
| `tests/test_moomoo_broker_offline.py` | Modify (Test) | `get_account` sets `positions_loaded` (Task 1). |
| `tests/test_sim_broker.py` | Modify (Test) | `SimBroker` reports loaded (Task 1). |
| `tests/test_db.py` | Modify (Test) | `record_performance` invariant + NULL gross (Task 2). |
| `tests/test_runner_report_integrity.py` | Create (Test) | EOD retry-with-backoff; realized/unrealized recording (Tasks 3, 6). |
| `tests/test_reporting_pnl.py` | Create (Test) | Running avg-cost realized helper (Task 5). |
| `tests/test_eod_reporter.py` | Modify (Test) | Stale-exposure render; unrealized-unavailable; capital-flow verb by sign (Tasks 4, 6, 7). |

---

### Task 1: `positions_loaded` distinction in `AccountSnapshot` + brokers

Carry the existing None-vs-empty position result as an explicit boolean on the snapshot so downstream code can tell "genuinely flat" from "query failed".

**Files:**
- Modify: `autotrader/domain.py` (`AccountSnapshot`, lines 166–198)
- Modify: `autotrader/moomoo_broker.py` (`get_account`, lines 262–284)
- Modify: `autotrader/sim_broker.py` (`get_account`, lines 76–80)
- Test: `tests/test_domain.py`, `tests/test_moomoo_broker_offline.py`, `tests/test_sim_broker.py`

**Interfaces:**
- Produces: `AccountSnapshot.positions_loaded: bool` (default `True`, keyword-compatible with all existing construction sites).
- Consumes: `MoomooBroker._positions() -> Optional[List[Position]]` (unchanged: `None` = failed, `[]` = flat).

Steps:

- [ ] Write failing test in `tests/test_domain.py` (append):
  ```python
  def test_account_snapshot_positions_loaded_defaults_true():
      snap = AccountSnapshot(cash=1.0, total_assets=1.0, day_pnl=0.0, stale=False)
      assert snap.positions_loaded is True

  def test_account_snapshot_positions_loaded_can_be_false():
      snap = AccountSnapshot(cash=1.0, total_assets=1.0, day_pnl=0.0, stale=True,
                             positions_loaded=False)
      assert snap.positions_loaded is False
      assert snap.positions == ()
  ```
- [ ] Run it, expect FAIL:
  ```
  python -m pytest tests/test_domain.py::test_account_snapshot_positions_loaded_defaults_true -q
  ```
  Expected: `TypeError: __init__() got an unexpected keyword argument 'positions_loaded'` (2 failed).
- [ ] Minimal implementation in `autotrader/domain.py` — add the field to `AccountSnapshot` after `stale`:
  ```python
  @dataclass(frozen=True)
  class AccountSnapshot:
      cash: float
      total_assets: float
      day_pnl: float
      stale: bool
      positions_loaded: bool = True   # False = position query FAILED (not flat)
      unrealized_pnl: float = 0.0
      positions: Tuple[Position, ...] = ()
  ```
- [ ] Run it, expect PASS:
  ```
  python -m pytest tests/test_domain.py -q
  ```
  Expected: all pass (existing + 2 new).
- [ ] Write failing test in `tests/test_moomoo_broker_offline.py` (append; follow the existing offline-broker fixture style that fakes `_trade`/`_c`). Add both branches:
  ```python
  def test_get_account_marks_positions_not_loaded_on_query_failure(monkeypatch):
      br = _offline_broker()  # existing helper building a MoomooBroker with fake _trade/_c
      monkeypatch.setattr(br, "_positions", lambda: None)
      snap = br.get_account()
      assert snap.positions_loaded is False
      assert snap.stale is True
      assert snap.positions == ()

  def test_get_account_marks_positions_loaded_when_flat(monkeypatch):
      br = _offline_broker()
      monkeypatch.setattr(br, "_positions", lambda: [])
      snap = br.get_account()
      assert snap.positions_loaded is True
  ```
  (If `tests/test_moomoo_broker_offline.py` has no `_offline_broker` helper yet, reuse whatever construction the file already uses to instantiate `MoomooBroker` with faked `accinfo_query`; the two tests only need `_positions` monkeypatched and `accinfo_query` returning a valid single-row frame.)
- [ ] Run it, expect FAIL:
  ```
  python -m pytest tests/test_moomoo_broker_offline.py::test_get_account_marks_positions_not_loaded_on_query_failure -q
  ```
  Expected: `AssertionError` on `assert snap.positions_loaded is False` — the field now exists (Task 1a) but `get_account` still lets it default to `True`, so a failed position query is not reflected (1 failed).
- [ ] Minimal implementation in `autotrader/moomoo_broker.py` `get_account()` — set `positions_loaded` in both return sites:
  ```python
          positions = self._positions()
          if positions is None:
              # Position query FAILED — never present a falsely-flat snapshot the
              # risk core would trust. Mark stale AND not-loaded (E1/R7).
              return AccountSnapshot(cash=cash, total_assets=total, day_pnl=pnl,
                                     stale=True, positions_loaded=False,
                                     unrealized_pnl=upnl, positions=())
          # A genuinely empty account with zero assets is also treated as stale.
          stale = (total == 0 and not positions)
          return AccountSnapshot(cash=cash, total_assets=total, day_pnl=pnl,
                                 stale=stale, positions_loaded=True,
                                 unrealized_pnl=upnl, positions=tuple(positions))
  ```
- [ ] Run it, expect PASS:
  ```
  python -m pytest tests/test_moomoo_broker_offline.py -q
  ```
  Expected: all pass.
- [ ] Add a characterization test in `tests/test_sim_broker.py` (append). `SimBroker.get_account()` never fails to load its in-memory positions, so `positions_loaded` is always `True` — this test is a guard against a future regression that flips it, not a red→green step (the default already satisfies it):
  ```python
  def test_sim_broker_account_reports_positions_loaded():
      from autotrader.sim_broker import SimBroker
      br = SimBroker(quotes={"US.AAPL": 200.0})
      snap = br.get_account()
      assert snap.positions_loaded is True and snap.stale is False
  ```
- [ ] Run it, expect PASS (it passes on the current default; the next step makes the flag explicit for clarity, not to fix a failure):
  ```
  python -m pytest tests/test_sim_broker.py::test_sim_broker_account_reports_positions_loaded -q
  ```
  Expected: 1 passed.
- [ ] Make the flag explicit in `autotrader/sim_broker.py` `get_account()` (documentation of intent; no behavior change):
  ```python
      def get_account(self) -> AccountSnapshot:
          positions = tuple(self._positions.values())
          return AccountSnapshot(cash=self._cash, total_assets=self._cash,
                                 day_pnl=0.0, stale=False, positions_loaded=True,
                                 unrealized_pnl=0.0, positions=positions)
  ```
- [ ] Run it, expect PASS:
  ```
  python -m pytest tests/test_sim_broker.py tests/test_domain.py tests/test_moomoo_broker_offline.py -q
  ```
  Expected: all pass.
- [ ] Commit:
  ```
  git add autotrader/domain.py autotrader/moomoo_broker.py autotrader/sim_broker.py tests/test_domain.py tests/test_moomoo_broker_offline.py tests/test_sim_broker.py
  git commit -m "feat(domain): carry positions_loaded on AccountSnapshot

None-vs-empty position result now surfaces as an explicit bool so the
recorder can refuse a false gross_exposure=0 when positions never loaded.

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
  ```

---

### Task 2: `record_performance` invariant + nullable `gross_exposure`

Make the performance row able to hold "exposure unknown" and refuse to persist a fabricated zero.

**Files:**
- Modify: `autotrader/db.py` (`_SCHEMA` performance table, lines 70–78; `record_performance`, lines 211–222)
- Test: `tests/test_db.py`

**Interfaces:**
- Produces: `DB.record_performance(day_pnl: float, total_assets: float, cash: float, gross_exposure: Optional[float], unrealized_pnl: float = 0.0, *, positions_loaded: bool = True) -> None`.
  - Raises `ValueError` when `positions_loaded is False and gross_exposure not in (None, 0)`? No — the invariant is the inverse: when positions did not load, `gross_exposure` must be `None`. If a caller passes `positions_loaded=False` with a numeric `gross_exposure` (including `0`), coerce to `None` and log; when `positions_loaded=True` and `gross_exposure is None`, that is an explicit "unknown" and is stored as NULL.
  - Concretely: **refuse to write a false 0** — if `positions_loaded is False`, the stored `gross_exposure` is `NULL` regardless of the numeric argument.

Steps:

- [ ] Write failing test in `tests/test_db.py` (append):
  ```python
  def test_record_performance_writes_null_gross_when_positions_not_loaded(tmp_path):
      db = _db(tmp_path)
      db.record_performance(day_pnl=0.0, total_assets=1000.0, cash=1000.0,
                            gross_exposure=0.0, positions_loaded=False)
      row = db._conn.execute("SELECT gross_exposure FROM performance").fetchone()
      assert row[0] is None, "false 0% must not be persisted when positions failed to load"
      db.close()

  def test_record_performance_accepts_explicit_none_gross(tmp_path):
      db = _db(tmp_path)
      db.record_performance(day_pnl=0.0, total_assets=1000.0, cash=1000.0,
                            gross_exposure=None, positions_loaded=False)
      row = db._conn.execute("SELECT gross_exposure FROM performance").fetchone()
      assert row[0] is None
      db.close()

  def test_record_performance_stores_real_gross_when_loaded(tmp_path):
      db = _db(tmp_path)
      db.record_performance(day_pnl=5.0, total_assets=1000.0, cash=900.0,
                            gross_exposure=100.0, positions_loaded=True)
      row = db._conn.execute("SELECT gross_exposure FROM performance").fetchone()
      assert row[0] == 100.0
      db.close()
  ```
- [ ] Run it, expect FAIL:
  ```
  python -m pytest tests/test_db.py::test_record_performance_writes_null_gross_when_positions_not_loaded -q
  ```
  Expected: `TypeError: record_performance() got an unexpected keyword argument 'positions_loaded'` (3 failed).
- [ ] Minimal implementation in `autotrader/db.py`:
  - Make the schema column nullable — change line 75 from `gross_exposure REAL NOT NULL,` to `gross_exposure REAL,`. Add a migration mirroring the existing `unrealized_pnl` guard in `__init__` (SQLite cannot drop NOT NULL in place; a warn-only note is fine since fresh DBs get the new schema and existing rows are unaffected by relaxing the constraint on *new* inserts — but to be safe, only relax for new DBs; existing DBs already tolerate numeric writes). Keep it simple: change `_SCHEMA` only (fresh DBs). Existing rows keep their non-null values; new NULL writes are only attempted on the new column definition, so on an old DB with `NOT NULL`, guard by writing NULL only when the column allows it. To avoid an old-DB `IntegrityError`, add an `__init__` migration that rebuilds the column nullability is heavyweight — instead, in `record_performance`, when the value is `None` on a legacy NOT NULL column, the INSERT would fail; so add this migration in `__init__` after the `unrealized_pnl` block:
    ```python
            # gross_exposure must be nullable to represent "exposure unknown"
            # (positions failed to load); legacy DBs created it NOT NULL.
            perf_sql = self._conn.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name='performance'"
            ).fetchone()
            if perf_sql and "gross_exposure REAL NOT NULL" in perf_sql[0]:
                self._conn.executescript(
                    "ALTER TABLE performance RENAME TO performance_old;"
                    + _PERFORMANCE_TABLE +
                    "INSERT INTO performance "
                    "(date,day_pnl,total_assets,cash,gross_exposure,unrealized_pnl,updated_at) "
                    "SELECT date,day_pnl,total_assets,cash,gross_exposure,unrealized_pnl,updated_at "
                    "FROM performance_old;"
                    "DROP TABLE performance_old;")
    ```
    where `_PERFORMANCE_TABLE` is the `CREATE TABLE IF NOT EXISTS performance (...)` block extracted from `_SCHEMA` into a module constant so it can be reused verbatim by the migration. Update `_SCHEMA` to reference it (or leave `_SCHEMA` as-is with the column now `REAL` nullable and define `_PERFORMANCE_TABLE` separately for the rebuild). Simplest concrete form: define
    ```python
    _PERFORMANCE_TABLE = """
    CREATE TABLE IF NOT EXISTS performance (
        date           TEXT PRIMARY KEY,
        day_pnl        REAL NOT NULL,
        total_assets   REAL NOT NULL,
        cash           REAL NOT NULL,
        gross_exposure REAL,
        unrealized_pnl REAL NOT NULL DEFAULT 0,
        updated_at     TEXT NOT NULL
    );
    """
    ```
    and drop the old `performance` block out of `_SCHEMA`, appending `_PERFORMANCE_TABLE` to the executed schema string in `__init__` (`self._conn.executescript(_SCHEMA + _PERFORMANCE_TABLE)`).
  - Rewrite `record_performance`:
    ```python
    def record_performance(self, day_pnl: float, total_assets: float,
                           cash: float, gross_exposure: Optional[float],
                           unrealized_pnl: float = 0.0, *,
                           positions_loaded: bool = True) -> None:
        # Invariant: never persist a fabricated gross_exposure when positions did
        # not load. A failed snapshot stores NULL ("exposure unknown"), never 0.
        if not positions_loaded:
            if gross_exposure not in (None, 0, 0.0):
                logger.warning(
                    "record_performance: coercing gross_exposure=%s to NULL "
                    "(positions_loaded is False)", gross_exposure)
            gross_exposure = None
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO performance "
                "(date,day_pnl,total_assets,cash,gross_exposure,unrealized_pnl,updated_at) "
                "VALUES (?,?,?,?,?,?,?)",
                (_today(), day_pnl, total_assets, cash, gross_exposure,
                 unrealized_pnl, _now()),
            )
            self._conn.commit()
    ```
    Add at module top (after imports): `import logging` and `logger = logging.getLogger("autotrader.db")`.
- [ ] Run it, expect PASS:
  ```
  python -m pytest tests/test_db.py tests/test_unrealized_snapshot.py -q
  ```
  Expected: all pass (including the existing `test_record_performance_persists_unrealized` / `_unrealized_defaults_zero`, which pass `gross_exposure=` positionally and omit `positions_loaded` → default `True`).
- [ ] Commit:
  ```
  git add autotrader/db.py tests/test_db.py
  git commit -m "feat(db): nullable gross_exposure + record_performance invariant

record_performance refuses to persist a fabricated gross_exposure=0 when
positions_loaded is false; stores NULL (exposure unknown) instead. Column
made nullable with a legacy-DB migration.

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
  ```

---

### Task 3: EOD retry-with-backoff on a stale/failed positions snapshot

When `_record_perf()` sees a snapshot whose positions failed to load, retry the account fetch with capped exponential backoff before recording; if still unresolved, record with `gross_exposure=NULL` + `positions_loaded=False`.

**Files:**
- Modify: `autotrader/runner.py` (`__init__` signature to accept an injected `sleep` for the retry — it already has `self._sleep`; `_record_perf`, lines 45–49)
- Test: `tests/test_runner_report_integrity.py` (Create)

**Interfaces:**
- Consumes: `autotrader.watchdog.backoff_seconds(attempt: int, base: float = 1.0, cap: float = 30.0) -> float`; `Broker.get_account() -> AccountSnapshot`; injected `self._sleep: Callable[[float], None]` (already a constructor arg).
- Produces: side-effecting `DB.record_performance(..., gross_exposure=Optional[float], positions_loaded=snap.positions_loaded)`.
- New private: `SessionRunner._fetch_account_for_perf(max_attempts: int = 4) -> AccountSnapshot` — returns the first snapshot with `positions_loaded is True`, or the last one after exhausting retries.

Steps:

- [ ] Write failing test in `tests/test_runner_report_integrity.py` (Create). Use a stub broker that fails positions N times then succeeds; assert backoff sleeps happened and the recorded gross is real once resolved, NULL when never resolved. Model the stub on `SimBroker`'s snapshot shape:
  ```python
  from autotrader.domain import AccountSnapshot, Position
  from autotrader.db import DB
  from autotrader.runner import SessionRunner


  class _AcctStub:
      """Broker stub: returns not-loaded snapshots for `fail_times` calls, then loaded."""
      def __init__(self, fail_times, loaded_positions=(), quotes=None):
          self.fail_times = fail_times
          self.calls = 0
          self._pos = tuple(loaded_positions)
          self._quotes = quotes or {}
      def get_account(self):
          self.calls += 1
          if self.calls <= self.fail_times:
              return AccountSnapshot(cash=1000.0, total_assets=1000.0, day_pnl=0.0,
                                     stale=True, positions_loaded=False, positions=())
          return AccountSnapshot(cash=1000.0, total_assets=1000.0, day_pnl=0.0,
                                 stale=False, positions_loaded=True, positions=self._pos)
      def get_quote(self, symbol):
          return self._quotes.get(symbol)


  def _runner(db, broker, sleeps):
      return SessionRunner(engine=None, broker=broker, db=db, gate=None,
                           scheduler=None, watchdog=None, clock=None,
                           sleep=sleeps.append, loop_interval=5.0)


  def test_record_perf_retries_then_records_real_gross(tmp_path):
      db = DB(str(tmp_path / "r.db"))
      br = _AcctStub(fail_times=2, loaded_positions=[Position("US.AAPL", 10, 100.0)],
                     quotes={"US.AAPL": 100.0})
      sleeps = []
      _runner(db, br, sleeps)._record_perf()
      assert br.calls == 3                      # 2 failed + 1 success
      assert sleeps == [1.0, 2.0]               # backoff before each retry
      row = db._conn.execute(
          "SELECT gross_exposure FROM performance").fetchone()
      assert row[0] == 1000.0                    # abs(10)*100 avg_price
      db.close()


  def test_record_perf_records_null_gross_when_never_resolved(tmp_path):
      db = DB(str(tmp_path / "r.db"))
      br = _AcctStub(fail_times=99)
      sleeps = []
      _runner(db, br, sleeps)._record_perf()
      assert br.calls == 4                       # 1 initial + 3 retries (max_attempts=4)
      assert sleeps == [1.0, 2.0, 4.0]
      row = db._conn.execute(
          "SELECT gross_exposure FROM performance").fetchone()
      assert row[0] is None
      db.close()
  ```
- [ ] Run it, expect FAIL:
  ```
  python -m pytest tests/test_runner_report_integrity.py -q
  ```
  Expected: `AssertionError` on `br.calls == 3` — current `_record_perf` calls `get_account()` exactly once and records `snap.gross_exposure()` unconditionally (2 failed).
- [ ] Minimal implementation in `autotrader/runner.py` — add the import and rewrite `_record_perf`:
  ```python
  from autotrader.watchdog import backoff_seconds
  ```
  ```python
      def _fetch_account_for_perf(self, max_attempts: int = 4):
          """Fetch the account snapshot for the performance row, retrying with
          capped exponential backoff while the position query keeps failing
          (positions_loaded is False). Returns the first loaded snapshot, or the
          last snapshot after exhausting attempts. Injected sleep — never a bare
          time.sleep as a readiness check (CLAUDE.md)."""
          snap = self._broker.get_account()
          attempt = 1
          while not snap.positions_loaded and attempt < max_attempts:
              delay = backoff_seconds(attempt)
              logger.warning("record_perf: positions unloaded — backoff %.1fs "
                             "(attempt %d/%d)", delay, attempt, max_attempts)
              self._sleep(delay)
              snap = self._broker.get_account()
              attempt += 1
          return snap

      def _record_perf(self) -> None:
          snap = self._fetch_account_for_perf()
          gross = snap.gross_exposure() if snap.positions_loaded else None
          self._db.record_performance(
              snap.day_pnl, snap.total_assets, snap.cash, gross,
              snap.unrealized_pnl, positions_loaded=snap.positions_loaded)
  ```
  (Note: `backoff_seconds(1)=1.0`, `(2)=2.0`, `(3)=4.0` — matches the asserted sleep sequences. With `max_attempts=4`: attempts 1,2,3 sleep, 4th is the last fetch → total 4 `get_account` calls, 3 sleeps.)
- [ ] Run it, expect PASS:
  ```
  python -m pytest tests/test_runner_report_integrity.py -q
  ```
  Expected: both pass.
- [ ] Run the runner suite to confirm no regression in existing `_record_perf` callers:
  ```
  python -m pytest tests/ -q -k runner
  ```
  Expected: all pass.
- [ ] Commit:
  ```
  git add autotrader/runner.py tests/test_runner_report_integrity.py
  git commit -m "feat(runner): retry account fetch with backoff before recording perf

On a failed position snapshot _record_perf now retries with capped
exponential backoff (watchdog.backoff_seconds, injected sleep); records
gross_exposure=NULL + positions_loaded=False when still unresolved.

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
  ```

---

### Task 4: Reporter renders NULL gross exposure as "exposure unavailable — snapshot stale"

The performance row can now hold `NULL` gross exposure; the reporter must render it as a visible-missing message, never `0%`, in both the text summary and the Block Kit fields.

**Files:**
- Modify: `autotrader/reporting/eod_reporter.py` (`_gross_pct` lines 176–178; `_header_lines` line 236; `_build_blocks` line 269)
- Test: `tests/test_eod_reporter.py`

**Interfaces:**
- Consumes: `ReportData.gross_exposure: Optional[float]` (already Optional; NULL from DB arrives as Python `None`).
- Produces: new `EODReporter._gross_text(d: ReportData) -> str` returning either `"{pct:.0f}%"` or `"exposure unavailable — snapshot stale"`.

Steps:

- [ ] Write failing test in `tests/test_eod_reporter.py` (append). Seed a performance row with NULL gross and assert the message appears and `0%` does not:
  ```python
  def _seed_null_gross(db):
      db._conn.execute(
          "INSERT INTO performance "
          "(date,day_pnl,total_assets,cash,gross_exposure,unrealized_pnl,updated_at) "
          "VALUES (?,?,?,?,?,?,?)",
          (_DAY, 12.0, 100000.0, 100000.0, None, 0.0, _DAY + "T20:30:00+00:00"))
      db._conn.commit()


  def test_render_null_gross_shows_unavailable(tmp_path):
      db = DB(str(tmp_path / "r.db"))
      _seed_null_gross(db)
      payload = _reporter(db, lambda u, p: 200)._render(
          _reporter(db, lambda u, p: 200)._gather(_now()))
      text = payload["text"]
      assert "exposure unavailable — snapshot stale" in text
      assert "Gross exp 0%" not in text
      # Block Kit gross field carries the same message, never "0%".
      flat = " ".join(
          f["text"] for b in payload["blocks"] if b.get("fields")
          for f in b["fields"])
      assert "exposure unavailable — snapshot stale" in flat
      db.close()
  ```
- [ ] Run it, expect FAIL:
  ```
  python -m pytest tests/test_eod_reporter.py::test_render_null_gross_shows_unavailable -q
  ```
  Expected: `AssertionError` — `_gross_pct` returns `0.0` when `gross_exposure is None`, so the text shows `Gross exp 0%` (1 failed).
- [ ] Minimal implementation in `autotrader/reporting/eod_reporter.py`:
  - Add a text helper next to `_gross_pct`:
    ```python
      _GROSS_UNAVAILABLE = "exposure unavailable — snapshot stale"

      @staticmethod
      def _gross_text(d: ReportData) -> str:
          if d.gross_exposure is None or d.total_assets is None:
              return EODReporter._GROSS_UNAVAILABLE
          pct = (d.gross_exposure / d.total_assets * 100) if d.total_assets else 0.0
          return f"{pct:.0f}%"
    ```
  - In `_header_lines`, change the `Gross exp {self._gross_pct(d):.0f}%` fragment to `Gross exp {self._gross_text(d)}`. Since a NULL-gross day may still have `total_assets`, keep the header block gated on `d.total_assets is not None` (unchanged). Concretely, replace line 236's `f"Gross exp {self._gross_pct(d):.0f}%")` with `f"Gross exp {self._gross_text(d)}")`.
  - In `_build_blocks`, replace the gross field (line 269):
    ```python
                {"type": "mrkdwn", "text": f"*Gross exp.*\n{self._gross_text(d)}"},
    ```
  - Leave `_gross_pct` in place only if still referenced; if nothing else uses it after this edit, delete it to avoid dead code.
- [ ] Run it, expect PASS:
  ```
  python -m pytest tests/test_eod_reporter.py -q
  ```
  Expected: all pass (existing `test_render_enriched_report` still shows `Gross exp 66%` because its seed writes numeric `66000.0`).
- [ ] Commit:
  ```
  git add autotrader/reporting/eod_reporter.py tests/test_eod_reporter.py
  git commit -m "feat(reporting): render NULL gross exposure as stale-unavailable

A performance row with gross_exposure NULL now renders 'exposure
unavailable — snapshot stale' in the header and Block Kit fields, never 0%.

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
  ```

---

### Task 5: Running average-cost realized-P&L helper

A pure, DB-free helper that walks a symbol's full fill history in chronological order, maintains a running average cost per symbol, and sums realized P&L for today's SELLs: `realized += (sell_price − avg_cost) × qty`. Average cost (not FIFO), per the design decision.

**Files:**
- Create: `autotrader/reporting/pnl.py`
- Test: `tests/test_reporting_pnl.py` (Create)

**Interfaces:**
- Produces:
  ```python
  # A fill row as read from the DB: (symbol, side, qty, price, ts)
  FillRow = Tuple[str, str, float, float, str]

  def realized_from_fills(fills: Iterable[FillRow], day: str) -> Optional[float]:
      """Running average-cost realized P&L for SELLs whose ts date == `day`.
      `fills` must be ALL fills for the involved symbols, chronological. Returns
      the summed realized P&L, or None if no SELL occurred on `day` (nothing
      derivable -> reporter renders '—', never a defaulted 0.00)."""
  ```
  Rules: iterate fills sorted by `ts`; for a BUY, update running avg cost `avg = (avg*held + price*qty) / (held+qty)` and `held += qty`; for a SELL, if its `ts[:10] == day` accumulate `(price − avg) × qty` into realized and mark that a same-day sell was seen; then reduce `held -= qty` (avg unchanged on sells, average-cost method). Return `realized` if any same-day sell was seen, else `None`. Guard `held == 0` on a BUY to avoid divide-by-zero.

Steps:

- [ ] Write failing test in `tests/test_reporting_pnl.py` (Create):
  ```python
  from autotrader.reporting.pnl import realized_from_fills

  DAY = "2026-06-16"


  def test_avg_cost_realized_single_symbol():
      # Buy 10@100 then 10@120 -> avg 110. Sell 5@130 today -> (130-110)*5 = 100.
      fills = [
          ("US.AAPL", "BUY", 10, 100.0, "2026-06-15T14:00:00+00:00"),
          ("US.AAPL", "BUY", 10, 120.0, "2026-06-16T14:00:00+00:00"),
          ("US.AAPL", "SELL", 5, 130.0, "2026-06-16T15:00:00+00:00"),
      ]
      assert abs(realized_from_fills(fills, DAY) - 100.0) < 1e-6


  def test_realized_none_when_no_sell_today():
      fills = [("US.AAPL", "BUY", 10, 100.0, "2026-06-16T14:00:00+00:00")]
      assert realized_from_fills(fills, DAY) is None


  def test_realized_ignores_prior_day_sells():
      fills = [
          ("US.AAPL", "BUY", 10, 100.0, "2026-06-14T14:00:00+00:00"),
          ("US.AAPL", "SELL", 10, 150.0, "2026-06-15T14:00:00+00:00"),  # prior day
      ]
      assert realized_from_fills(fills, DAY) is None


  def test_realized_multi_symbol_summed():
      fills = [
          ("US.AAPL", "BUY", 10, 100.0, "2026-06-16T14:00:00+00:00"),
          ("US.AAPL", "SELL", 10, 110.0, "2026-06-16T15:00:00+00:00"),   # +100
          ("US.MARA", "BUY", 100, 13.0, "2026-06-16T14:00:00+00:00"),
          ("US.MARA", "SELL", 100, 12.60, "2026-06-16T15:00:00+00:00"),  # -40
      ]
      assert abs(realized_from_fills(fills, DAY) - 60.0) < 1e-6
  ```
- [ ] Run it, expect FAIL:
  ```
  python -m pytest tests/test_reporting_pnl.py -q
  ```
  Expected: `ModuleNotFoundError: No module named 'autotrader.reporting.pnl'` (collection error / 4 failed).
- [ ] Minimal implementation in `autotrader/reporting/pnl.py` (Create):
  ```python
  """Pure realized-P&L from fills using a running AVERAGE-COST basis per symbol
  (not FIFO — matches Position.avg_price). DB-free: the reporter/runner passes
  in fill rows. Never raises on ordinary inputs."""
  from __future__ import annotations

  from typing import Iterable, Optional, Tuple

  FillRow = Tuple[str, str, float, float, str]  # (symbol, side, qty, price, ts)


  def realized_from_fills(fills: Iterable[FillRow], day: str) -> Optional[float]:
      rows = sorted(fills, key=lambda r: r[4])
      held: dict = {}   # symbol -> shares held
      avg: dict = {}    # symbol -> running average cost
      realized = 0.0
      saw_sell_today = False
      for symbol, side, qty, price, ts in rows:
          q = float(qty)
          if side == "BUY":
              h = held.get(symbol, 0.0)
              a = avg.get(symbol, 0.0)
              new_h = h + q
              avg[symbol] = ((a * h) + (price * q)) / new_h if new_h else 0.0
              held[symbol] = new_h
          else:  # SELL
              a = avg.get(symbol, 0.0)
              if ts[:10] == day:
                  realized += (price - a) * q
                  saw_sell_today = True
              held[symbol] = held.get(symbol, 0.0) - q  # avg unchanged (avg-cost)
      return realized if saw_sell_today else None
  ```
- [ ] Run it, expect PASS:
  ```
  python -m pytest tests/test_reporting_pnl.py -q
  ```
  Expected: 4 passed.
- [ ] Commit:
  ```
  git add autotrader/reporting/pnl.py tests/test_reporting_pnl.py
  git commit -m "feat(reporting): running average-cost realized P&L helper

Pure realized_from_fills computes day realized P&L from full fill history
using average-cost basis per symbol; returns None (not 0) when no same-day
sell exists so the reporter can render '—'.

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
  ```

---

### Task 6: Wire computed realized/unrealized; render "—"/"unavailable"

Compute realized (from DB fills via Task 5) and unrealized (open positions × current quote) at record time in the runner, persist them, and have the reporter render `—` for a `None` realized and "unavailable" for unrealized when the snapshot was stale/unloaded. Broker fields are the fallback.

**Files:**
- Modify: `autotrader/runner.py` (`_record_perf` — realized override from fills, unrealized from positions × quote)
- Modify: `autotrader/reporting/eod_reporter.py` (`_header_lines` lines 228–236, `_build_blocks` lines 262–271: realized `—` when None, unrealized "unavailable" when gross is NULL/stale)
- Test: `tests/test_runner_report_integrity.py`, `tests/test_eod_reporter.py`

**Interfaces:**
- Consumes in runner: `autotrader.reporting.pnl.realized_from_fills`; `Broker.get_quote(symbol) -> Optional[float]`; `AccountSnapshot.positions: Tuple[Position, ...]`, `.unrealized_pnl`, `.positions_loaded`.
- DB read for fills: `SELECT symbol, side, qty, price, ts FROM fills` (all rows — the helper needs full history).
- Produces: `record_performance(day_pnl=<realized or broker day_pnl>, ..., unrealized_pnl=<computed or broker upnl>, positions_loaded=...)`. When positions did not load, unrealized is not recomputed (kept as broker `upnl`); the reporter renders "unavailable" for it whenever `gross_exposure IS NULL` (the stale signal).
- Reporter render: realized `—` when `ReportData.realized_pnl is None`; unrealized `"unavailable"` when `ReportData.gross_exposure is None` (stale), else the numeric value or `—` when `None`.

Steps:

- [ ] Write failing test in `tests/test_runner_report_integrity.py` (append) — realized computed from fills, unrealized from positions × quote:
  ```python
  from autotrader.domain import Fill

  def test_record_perf_computes_realized_and_unrealized(tmp_path):
      db = DB(str(tmp_path / "r.db"))
      # Seed fills so realized_from_fills has history: buy 10@100 today, sell 10@110 today.
      db.record_fills([
          Fill("f1", "US.AAPL", "BUY", 10, 100.0, _today_iso() + "T14:00:00+00:00"),
          Fill("f2", "US.AAPL", "SELL", 10, 110.0, _today_iso() + "T15:00:00+00:00"),
      ])
      # Open position after: none for AAPL; add a held name for unrealized.
      br = _AcctStub(fail_times=0,
                     loaded_positions=[Position("US.MARA", 100, 13.0)],
                     quotes={"US.MARA": 13.60})
      _runner(db, br, [])._record_perf()
      row = db._conn.execute(
          "SELECT day_pnl, unrealized_pnl FROM performance").fetchone()
      assert abs(row[0] - 100.0) < 1e-6          # realized from fills
      assert abs(row[1] - 60.0) < 1e-6           # 100 * (13.60 - 13.00)
      db.close()
  ```
  Add a `_today_iso()` helper at the top of the test module:
  ```python
  from datetime import date
  def _today_iso():
      return date.today().isoformat()
  ```
- [ ] Run it, expect FAIL:
  ```
  python -m pytest tests/test_runner_report_integrity.py::test_record_perf_computes_realized_and_unrealized -q
  ```
  Expected: `AssertionError` — current `_record_perf` records `snap.day_pnl` (0.0 from the stub) and `snap.unrealized_pnl` (0.0), not computed values (1 failed).
- [ ] Minimal implementation in `autotrader/runner.py` — extend `_record_perf` to compute both:
  ```python
  from datetime import date as _date
  from autotrader.reporting.pnl import realized_from_fills
  ```
  ```python
      def _compute_unrealized(self, snap) -> float:
          """Σ qty × (current_quote − avg_cost) over open positions using the
          same snapshot. Positions with no live quote are skipped."""
          total = 0.0
          for p in snap.positions:
              quote = self._broker.get_quote(p.symbol)
              if quote is not None:
                  total += p.qty * (quote - p.avg_price)
          return total

      def _compute_realized(self) -> "float | None":
          rows = self._db._conn.execute(
              "SELECT symbol, side, qty, price, ts FROM fills").fetchall()
          return realized_from_fills(rows, _date.today().isoformat())

      def _record_perf(self) -> None:
          snap = self._fetch_account_for_perf()
          gross = snap.gross_exposure() if snap.positions_loaded else None
          # Realized: prefer our own fills-derived figure; fall back to broker day_pnl.
          realized = self._compute_realized()
          day_pnl = realized if realized is not None else snap.day_pnl
          # Unrealized: recompute from positions × quote only when positions loaded;
          # otherwise keep the broker figure (reporter renders it 'unavailable').
          unreal = self._compute_unrealized(snap) if snap.positions_loaded \
              else snap.unrealized_pnl
          self._db.record_performance(
              day_pnl, snap.total_assets, snap.cash, gross, unreal,
              positions_loaded=snap.positions_loaded)
  ```
  (Note: the reporter reads `day_pnl` into `ReportData.realized_pnl`; storing the computed realized in the existing `day_pnl` column keeps the schema unchanged and matches the reporter's `_gather` mapping `realized_pnl=perf[0]`.)
- [ ] Run it, expect PASS:
  ```
  python -m pytest tests/test_runner_report_integrity.py -q
  ```
  Expected: all pass.
- [ ] Write failing reporter test in `tests/test_eod_reporter.py` (append) — realized `—` when NULL day_pnl, unrealized "unavailable" when gross NULL:
  ```python
  def test_render_realized_dash_when_null(tmp_path):
      db = DB(str(tmp_path / "r.db"))
      db._conn.execute(
          "INSERT INTO performance "
          "(date,day_pnl,total_assets,cash,gross_exposure,unrealized_pnl,updated_at) "
          "VALUES (?,?,?,?,?,?,?)",
          (_DAY, None, 100000.0, 100000.0, 66000.0, 0.0, _DAY + "T20:30:00+00:00"))
      db._conn.commit()
      text = _reporter(db, lambda u, p: 200)._render(
          _reporter(db, lambda u, p: 200)._gather(_now()))["text"]
      assert "Realized —" in text
      assert "+0.00" not in text.split("Assets")[0]   # no defaulted realized 0.00

  def test_render_unrealized_unavailable_when_stale(tmp_path):
      db = DB(str(tmp_path / "r.db"))
      db._conn.execute(
          "INSERT INTO performance "
          "(date,day_pnl,total_assets,cash,gross_exposure,unrealized_pnl,updated_at) "
          "VALUES (?,?,?,?,?,?,?)",
          (_DAY, 5.0, 100000.0, 100000.0, None, 0.0, _DAY + "T20:30:00+00:00"))
      db._conn.commit()
      text = _reporter(db, lambda u, p: 200)._render(
          _reporter(db, lambda u, p: 200)._gather(_now()))["text"]
      assert "Unrealized unavailable" in text
  ```
  This requires the reporter to distinguish a stored NULL `day_pnl` (→ `realized_pnl=None`) from `0.0`. The existing `_gather` maps `realized_pnl=perf[0] if perf else None`; a NULL cell already arrives as Python `None`, so `realized_pnl is None` is the correct signal.
- [ ] Run it, expect FAIL:
  ```
  python -m pytest tests/test_eod_reporter.py::test_render_realized_dash_when_null tests/test_eod_reporter.py::test_render_unrealized_unavailable_when_stale -q
  ```
  Expected: `AssertionError` — `_header_lines` does `realized = d.realized_pnl or 0.0` (renders `+0.00`) and always shows the numeric unrealized (2 failed).
- [ ] Minimal implementation in `autotrader/reporting/eod_reporter.py`:
  - Add render helpers:
    ```python
      @staticmethod
      def _realized_text(d: ReportData) -> str:
          return "—" if d.realized_pnl is None else f"{d.realized_pnl:+,.2f}"

      @staticmethod
      def _unrealized_text(d: ReportData) -> str:
          if d.gross_exposure is None:          # snapshot stale -> not trustworthy
              return "unavailable"
          if d.unrealized_pnl is None:
              return "—"
          return f"{d.unrealized_pnl:+,.2f}"
    ```
  - Rewrite the header P&L fragment in `_header_lines` (replace the `realized`/`unreal` lines 231–235):
    ```python
            pct = "" if d.realized_pnl is None else f" ({self._pnl_pct(d):+.2f}%)"
            lines.append(
                f"Realized {self._realized_text(d)}{pct} · "
                f"Unrealized {self._unrealized_text(d)} · "
                f"Assets {d.total_assets:,.0f} · Cash {d.cash:,.0f} · "
                f"Gross exp {self._gross_text(d)}")
    ```
  - Rewrite the Block Kit realized/unrealized fields in `_build_blocks` (replace lines 264–266):
    ```python
            pct = "" if d.realized_pnl is None else f" ({self._pnl_pct(d):+.2f}%)"
            fields = [
                {"type": "mrkdwn", "text": f"*Realized*\n{self._realized_text(d)}{pct}"},
                {"type": "mrkdwn", "text": f"*Unrealized*\n{self._unrealized_text(d)}"},
                {"type": "mrkdwn", "text": f"*Total assets*\n{d.total_assets:,.0f}"},
                {"type": "mrkdwn", "text": f"*Cash*\n{d.cash:,.0f}"},
                {"type": "mrkdwn", "text": f"*Gross exp.*\n{self._gross_text(d)}"},
            ]
    ```
  - `_pnl_pct` is unchanged and only called when `realized_pnl is not None`.
- [ ] Run it, expect PASS:
  ```
  python -m pytest tests/test_eod_reporter.py -q
  ```
  Expected: all pass. Confirm the pre-existing `test_render_enriched_report` (`+842.13`, `Unrealized`) still passes — it seeds numeric `day_pnl=842.13`, `gross_exposure=66000.0`, so `_realized_text` → `+842.13` and `_unrealized_text` → `+0.00`.
- [ ] Commit:
  ```
  git add autotrader/runner.py autotrader/reporting/eod_reporter.py tests/test_runner_report_integrity.py tests/test_eod_reporter.py
  git commit -m "feat(reporting): compute realized/unrealized, render dash/unavailable

Runner computes day realized from fills (avg-cost) and unrealized from
positions × quote and persists them; reporter renders '—' for a null
realized and 'unavailable' for unrealized when the snapshot is stale,
never a defaulted 0.00. Broker fields remain the fallback.

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
  ```

---

### Task 7: Capital-flow verb by sign ("raised" / "deployed")

The `net_cash_deployed` value is correct; only the label is inverted. Render "Net cash raised +N" when positive (SELL > BUY, cash raised) and "Net cash deployed −N" when negative. No change to `capital_flow.py`'s formula.

**Files:**
- Modify: `autotrader/reporting/eod_reporter.py` (`_header_lines` capital-flow line 239–242; `_build_blocks` capital-flow section 272–277)
- Test: `tests/test_eod_reporter.py`

**Interfaces:**
- Consumes: `CapitalFlow.net_cash_deployed: float` (unchanged sign: SELL +, BUY −).
- Produces: `EODReporter._capital_flow_verb(net: float) -> str` returning the full `"Net cash raised +N"` / `"Net cash deployed −N"` fragment. The magnitude is `abs(net)`, with an explicit `+`/`−` sign for readability.

Steps:

- [ ] Write failing test in `tests/test_eod_reporter.py` (append). Seed a SELL-heavy day (positive net → raised) and a BUY-heavy day (negative net → deployed):
  ```python
  def _seed_flow(db, fills):
      db._conn.execute(
          "INSERT INTO performance "
          "(date,day_pnl,total_assets,cash,gross_exposure,unrealized_pnl,updated_at) "
          "VALUES (?,?,?,?,?,?,?)",
          (_DAY, 0.0, 100000.0, 100000.0, 1000.0, 0.0, _DAY + "T20:30:00+00:00"))
      for fid, sym, side, qty, px in fills:
          db._conn.execute(
              "INSERT INTO fills (fill_id,ts,symbol,side,qty,price) VALUES (?,?,?,?,?,?)",
              (fid, _DAY + "T14:00:00+00:00", sym, side, qty, px))
      db._conn.commit()


  def test_capital_flow_label_raised_when_positive(tmp_path):
      db = DB(str(tmp_path / "r.db"))
      _seed_flow(db, [("s1", "US.SCHF", "SELL", 100, 28.27)])   # +2827 net
      text = _reporter(db, lambda u, p: 200)._render(
          _reporter(db, lambda u, p: 200)._gather(_now()))["text"]
      assert "Net cash raised +2,827" in text
      assert "deployed" not in text.lower()
      db.close()


  def test_capital_flow_label_deployed_when_negative(tmp_path):
      db = DB(str(tmp_path / "r.db"))
      _seed_flow(db, [("b1", "US.AAPL", "BUY", 100, 20.0)])     # -2000 net
      text = _reporter(db, lambda u, p: 200)._render(
          _reporter(db, lambda u, p: 200)._gather(_now()))["text"]
      assert "Net cash deployed −2,000" in text                 # U+2212 minus sign
      assert "raised" not in text.lower()
      db.close()
  ```
- [ ] Run it, expect FAIL:
  ```
  python -m pytest tests/test_eod_reporter.py::test_capital_flow_label_raised_when_positive tests/test_eod_reporter.py::test_capital_flow_label_deployed_when_negative -q
  ```
  Expected: `AssertionError` — current code always prints `Net cash deployed {net:+,.0f}` (so the raised-day shows `Net cash deployed +2,827`, failing) (2 failed).
- [ ] Minimal implementation in `autotrader/reporting/eod_reporter.py`:
  - Add the verb helper:
    ```python
      @staticmethod
      def _capital_flow_verb(net: float) -> str:
          # Value/formula unchanged; only the verb follows the sign.
          if net >= 0:
              return f"Net cash raised +{net:,.0f}"
          return f"Net cash deployed −{abs(net):,.0f}"
    ```
  - In `_header_lines`, replace the `f"Net cash deployed {cf.net_cash_deployed:+,.0f}"` fragment (line 242) with `f"{self._capital_flow_verb(cf.net_cash_deployed)}"`.
  - In `_build_blocks`, replace the same fragment in the capital-flow section (line 277) with `f"{self._capital_flow_verb(cf.net_cash_deployed)}"`.
- [ ] Run it, expect PASS:
  ```
  python -m pytest tests/test_eod_reporter.py -q
  ```
  Expected: all pass. Note the pre-existing `test_render_enriched_report` asserts `"Net cash deployed"` in text — the CLOV/AAPL seed there is BUY-heavy (net negative), so `_capital_flow_verb` still emits "Net cash deployed", keeping that assertion green. **Verify this**: if the enriched seed's net is positive, update that assertion in the same commit to `"Net cash"` (still true for both verbs). Compute the seed net before finalizing.
- [ ] Run the full reporting + integrity suite:
  ```
  python -m pytest tests/test_eod_reporter.py tests/test_reporting_capital_flow.py tests/test_reporting_pnl.py tests/test_runner_report_integrity.py tests/test_db.py tests/test_domain.py -q
  ```
  Expected: all pass.
- [ ] Commit:
  ```
  git add autotrader/reporting/eod_reporter.py tests/test_eod_reporter.py
  git commit -m "feat(reporting): capital-flow verb by sign (raised/deployed)

Positive net (SELL > BUY) renders 'Net cash raised +N'; negative renders
'Net cash deployed −N'. Value and formula unchanged.

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
  ```

---

## Final verification

- [ ] Run the entire offline suite to confirm zero regressions across the branch:
  ```
  python -m pytest tests/ -q
  ```
  Expected: all pass, 0 skipped (the branch baseline was 118 passing; this plan adds tests and must not reduce that count).
