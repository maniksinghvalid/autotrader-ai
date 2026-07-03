# Live-Readiness v2 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close all C1–C6 Criticals and live-blocking Importants from the 2026-07-02 three-lens review (spec: `docs/superpowers/specs/2026-07-02-live-readiness-v2-design.md`) so live cutover can proceed in stages.

**Architecture:** V1 adds an async-fill mode to SimBroker so live-only failure modes become offline regression tests. V2–V5 harden the core loop (scheduler mark-on-success, fill-confirmed stops, fail-closed halts, cancel-confirm escalation). V6–V7 harden ingress and options. V11 makes account sovereignty explicit (SOLE|SHARED) so AutoTrader coexists with the SNP bot on the shared paper account. V8–V10 add alerting, supervision, and docs.

**Tech Stack:** Python 3.14, pytest, Flask, pydantic, sqlite3, launchd. Run tests with `uv run pytest`.

## Global Constraints

- Every Moomoo SDK call checks `ret_code == RET_OK` (via `MoomooBroker._ok`); never swallow non-OK.
- Never call `unlock_trade` or write code that does.
- No hardcoded hosts/ports/credentials/risk values outside `config/` / env (`RISK_*`, `AUTOTRADER_*`, `FUTU_*`).
- No `time.sleep()` as a readiness check — injected sleep + `watchdog.backoff_seconds` pattern only.
- All new tests must run offline (no OpenD, no SDK import in core — `tests/test_no_sdk_in_core.py` enforces this).
- The full offline suite must stay green after every task: `uv run pytest` (587 passed, 2 skipped at plan start; count grows).
- Work on branch `feat/live-readiness-v2` off `develop`. Do NOT push; no PR until the whole plan is approved.
- Implementer subagents must NOT spawn nested agents.
- Commit format: `type(scope): summary` + `Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>`.

---

### Task 1: V1 — SimBroker async-fill + cancel-latency mode

**Files:**
- Modify: `autotrader/sim_broker.py`
- Test: `tests/test_sim_broker_async.py` (new)

**Interfaces:**
- Consumes: existing `SimBroker`, `OrderAck`, `OrderState`, `Fill`.
- Produces: `SimBroker(..., fill_latency_ticks: int = 0, cancel_latency_ticks: int = 0)` and `SimBroker.tick_market() -> None`. With `fill_latency_ticks=0` (default) behavior is byte-for-byte today's. With `fill_latency_ticks=N`, MARKET and marketable-LIMIT orders ack `SUBMITTED`, appear in `get_open_orders()`, and fill only after N `tick_market()` calls. `cancel_order` with `cancel_latency_ticks=M` records a pending cancel that takes effect after M ticks; **within one `tick_market()`, pending fills mature before pending cancels**, so a fill due the same tick as a cancel wins (the live race, scriptable). Tasks 4, 9, 10 test against this.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_sim_broker_async.py
"""V1 rig: async fills + cancel latency make live-only races testable offline."""
from autotrader.domain import OrderRequest, OrderState
from autotrader.sim_broker import SimBroker


def _buy(cid="c1", qty=10):
    return OrderRequest(symbol="US.TEST", side="BUY", qty=qty,
                        order_type="MARKET", limit_price=None, client_order_id=cid)


def test_sync_default_unchanged():
    b = SimBroker({"US.TEST": 100.0})
    ack = b.place_order(_buy())
    assert ack.state is OrderState.FILLED          # today's behavior preserved


def test_async_market_acks_submitted_then_fills_after_latency():
    b = SimBroker({"US.TEST": 100.0}, fill_latency_ticks=2)
    ack = b.place_order(_buy())
    assert ack.state is OrderState.SUBMITTED
    assert any(o.client_order_id == "c1" for o in b.get_open_orders())
    assert b.get_account().position_qty("US.TEST") == 0
    b.tick_market()
    assert any(o.client_order_id == "c1" for o in b.get_open_orders())  # still working
    b.tick_market()
    assert not any(o.client_order_id == "c1" for o in b.get_open_orders())
    assert b.get_account().position_qty("US.TEST") == 10
    fills = b.reconcile_fills(None)
    assert len(fills) == 1 and fills[0].symbol == "US.TEST"


def test_cancel_latency_fill_wins_race():
    # Fill due on the same tick as the cancel -> fill wins (live race).
    b = SimBroker({"US.TEST": 100.0}, fill_latency_ticks=1, cancel_latency_ticks=1)
    ack = b.place_order(_buy())
    b.cancel_order(ack.broker_order_id)   # cancel REQUEST accepted, not effective yet
    assert any(o.client_order_id == "c1" for o in b.get_open_orders())
    b.tick_market()                        # fill matures BEFORE cancel
    assert b.get_account().position_qty("US.TEST") == 10   # double-fill hazard made visible


def test_cancel_before_fill_matures_kills_order():
    b = SimBroker({"US.TEST": 100.0}, fill_latency_ticks=3, cancel_latency_ticks=1)
    ack = b.place_order(_buy())
    b.cancel_order(ack.broker_order_id)
    b.tick_market()                        # cancel matures (fill needs 2 more)
    assert not any(o.client_order_id == "c1" for o in b.get_open_orders())
    b.tick_market(); b.tick_market()
    assert b.get_account().position_qty("US.TEST") == 0    # never fills
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_sim_broker_async.py -v`
Expected: FAIL — `TypeError: __init__() got an unexpected keyword argument 'fill_latency_ticks'`

- [ ] **Step 3: Implement**

In `SimBroker.__init__`, accept and store the two params, plus pending books:

```python
                 recent_highs: Optional[Dict[str, float]] = None,
                 fill_latency_ticks: int = 0, cancel_latency_ticks: int = 0):
        ...
        # V1 async rig: 0 = synchronous (today's behavior). >0 = orders ack
        # SUBMITTED and fill/cancel only after N tick_market() calls; within a
        # tick, fills mature BEFORE cancels (the live cancel-race, deterministic).
        self._fill_latency = int(fill_latency_ticks)
        self._cancel_latency = int(cancel_latency_ticks)
        self._pending_fills: Dict[str, dict] = {}    # boid -> {req, price, left}
        self._pending_cancels: Dict[str, int] = {}   # boid -> ticks left
```

In `place_order`, route immediate-fill orders through the pending book when async:

```python
        else:  # MARKET (and marketable LIMIT falls through the branch above)
            rests = False
            price = self._market_fill_price(req.symbol, req.side)
        if not rests and self._fill_latency > 0:
            ack = OrderAck(req.client_order_id, boid, OrderState.SUBMITTED, {})
            self._open[boid] = ack
            self._pending_fills[boid] = {"req": req, "price": price,
                                         "left": self._fill_latency}
            self._acks_by_cid[req.client_order_id] = ack
            return ack
        if not rests:
            ...  # existing immediate-fill body unchanged
```

Also handle the marketable-LIMIT branch: when `self._fill_latency > 0` a marketable limit takes the same pending path (price = `req.limit_price`).

`cancel_order` honors latency (and never cancels an already-matured fill):

```python
    def cancel_order(self, broker_order_id: str) -> None:
        if broker_order_id not in self._open:
            return
        if self._cancel_latency > 0:
            self._pending_cancels[broker_order_id] = self._cancel_latency
            return
        self._open.pop(broker_order_id, None)
        self._pending_fills.pop(broker_order_id, None)
```

New `tick_market` — fills before cancels:

```python
    def tick_market(self) -> None:
        """Advance one sim tick: mature pending fills FIRST, then pending
        cancels — a fill and cancel due the same tick resolves as a fill
        (the live race a cancel-then-resubmit path must survive)."""
        for boid in list(self._pending_fills):
            entry = self._pending_fills[boid]
            entry["left"] -= 1
            if entry["left"] > 0:
                continue
            req, price = entry["req"], entry["price"]
            signed = req.qty if req.side == "BUY" else -req.qty
            self._cash -= signed * price
            prev = self._positions.get(req.symbol)
            new_qty = (prev.qty if prev else 0) + signed
            self._positions[req.symbol] = Position(req.symbol, new_qty, price)
            self._seq += 1
            self._fills.append(Fill(fill_id=f"fill-{self._seq}", symbol=req.symbol,
                                    side=req.side, qty=req.qty, price=price,
                                    ts=f"t{self._seq}"))
            del self._pending_fills[boid]
            self._open.pop(boid, None)
            self._pending_cancels.pop(boid, None)   # fill won the race
        for boid in list(self._pending_cancels):
            self._pending_cancels[boid] -= 1
            if self._pending_cancels[boid] <= 0:
                del self._pending_cancels[boid]
                self._open.pop(boid, None)
                self._pending_fills.pop(boid, None)
```

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/test_sim_broker_async.py tests/ -q`
Expected: new tests PASS, full suite still green.

- [ ] **Step 5: Commit**

```bash
git add autotrader/sim_broker.py tests/test_sim_broker_async.py
git commit -m "feat(sim): async-fill + cancel-latency mode (V1 live-shaped rig)"
```

---

### Task 2: V8a — AlertSink (Slack alert helper with episode dedup)

**Files:**
- Create: `autotrader/alerts.py`
- Test: `tests/test_alerts.py` (new)

**Interfaces:**
- Consumes: `autotrader.reporting.eod_reporter.post_slack` (existing `post_slack(url, payload) -> int` returning HTTP status).
- Produces: `AlertSink(url: Optional[str], post=None)` with `send(text: str, key: Optional[str] = None) -> bool` and `reset(key: str) -> None`. `send` never raises; returns True on 2xx; with `url=None` it is a logged no-op returning False. A repeated `key` is suppressed (once-per-episode) until `reset(key)`. Tasks 3, 8, 18 consume this.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_alerts.py
from autotrader.alerts import AlertSink


def test_send_posts_and_returns_true_on_2xx():
    posted = []
    sink = AlertSink("http://x", post=lambda url, payload: posted.append(payload) or 200)
    assert sink.send("hello") is True
    assert posted == [{"text": "hello"}]


def test_no_url_is_noop_false():
    assert AlertSink(None).send("hello") is False


def test_post_exception_never_raises():
    def boom(url, payload):
        raise OSError("network down")
    assert AlertSink("http://x", post=boom).send("hello") is False


def test_key_dedupes_until_reset():
    posted = []
    sink = AlertSink("http://x", post=lambda u, p: posted.append(p) or 200)
    assert sink.send("watchdog down", key="watchdog") is True
    assert sink.send("watchdog down", key="watchdog") is False   # suppressed
    assert len(posted) == 1
    sink.reset("watchdog")
    assert sink.send("watchdog down again", key="watchdog") is True
    assert len(posted) == 2
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_alerts.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'autotrader.alerts'`

- [ ] **Step 3: Implement**

```python
# autotrader/alerts.py
"""Best-effort Slack alerting for operational events (HALT, watchdog death,
job failures). NEVER raises into the trading loop; `key` gives once-per-episode
dedup so a repeating condition (e.g. HALTED_UNHEALTHY every 5s iteration)
alerts exactly once until reset. No SDK import."""
from __future__ import annotations

import logging
from typing import Callable, Optional, Set

logger = logging.getLogger("autotrader.alerts")


def _default_post(url: str, payload: dict) -> int:
    from autotrader.reporting.eod_reporter import post_slack  # lazy: keep import cheap
    return post_slack(url, payload)


class AlertSink:
    def __init__(self, url: Optional[str], post: Optional[Callable] = None):
        self._url = url
        self._post = post or _default_post
        self._fired: Set[str] = set()

    def send(self, text: str, key: Optional[str] = None) -> bool:
        if key is not None and key in self._fired:
            return False
        if self._url is None:
            logger.warning("ALERT (no Slack URL configured): %s", text)
            return False
        try:
            status = self._post(self._url, {"text": text})
        except Exception as e:            # alerting must never break the loop
            logger.error("alert POST failed: %s (text=%r)", e, text)
            return False
        if not (200 <= status < 300):
            logger.error("alert POST non-2xx: %s (text=%r)", status, text)
            return False
        if key is not None:
            self._fired.add(key)
        return True

    def reset(self, key: str) -> None:
        self._fired.discard(key)
```

- [ ] **Step 4: Run tests** — `uv run pytest tests/test_alerts.py tests/ -q` → PASS, suite green.

- [ ] **Step 5: Commit**

```bash
git add autotrader/alerts.py tests/test_alerts.py
git commit -m "feat(alerts): AlertSink with once-per-episode dedup (V8 plumbing)"
```

---

### Task 3: V2 — Scheduler mark-on-success + per-job isolation (C1)

**Files:**
- Modify: `autotrader/scheduler.py`, `autotrader/runner.py`
- Test: `tests/test_scheduler_mark_on_success.py` (new); update `tests/test_scheduler*.py` / runner tests that assume poll() self-marks.

**Interfaces:**
- Consumes: `LifecycleScheduler` (state_get/state_set), `SessionRunner._run_job`, `AlertSink` (Task 2).
- Produces: `LifecycleScheduler.poll(now) -> List[str]` now returns due-and-unfired jobs **without marking**; new `LifecycleScheduler.mark_fired(name: str, now: datetime) -> None` records `_last_fired` and persists AT_MOST_ONCE state. `SessionRunner.__init__` gains keyword `alerts: Optional[AlertSink] = None`. Runner marks each job fired only after `_run_job` returns; a raising job is logged (`exc_info=True`), alerted, and **retried on the next poll**; later due jobs in the same batch still run.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_scheduler_mark_on_success.py
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
```

Note for the implementer: with `db=None` the runner skips `ground_truth_sync`, so ALSO add a variant wiring a minimal `db` stub (`replace_positions`/`record_fills`/`get_state`/`set_state` no-ops) so `PRE_OPEN_SYNC` genuinely raises via `_BoomBroker.get_account`, and assert: (a) `gate.entries_enabled` is True (ENTRY_OPEN ran after the failure), (b) `PRE_OPEN_SYNC in runner._sched.poll(now)` (unmarked → retries), (c) `posted` contains one alert mentioning `PRE_OPEN_SYNC`.

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_scheduler_mark_on_success.py -v`
Expected: FAIL — `AttributeError: 'LifecycleScheduler' object has no attribute 'mark_fired'` / unexpected keyword `alerts`.

- [ ] **Step 3: Implement scheduler change**

```python
    def poll(self, now: datetime) -> List[str]:
        """Jobs due and not yet fired today. Does NOT mark — callers call
        mark_fired(name, now) after the job SUCCEEDS (C1: a failed job must
        retry next poll, and one failure must not consume the batch)."""
        today = now.date().isoformat()
        return [name for name, sched in _SCHEDULE
                if now.time() >= sched and self._last(name) != today]

    def mark_fired(self, name: str, now: datetime) -> None:
        today = now.date().isoformat()
        self._last_fired[name] = today
        if name in AT_MOST_ONCE and self._state_set is not None:
            self._state_set(f"sched:{name}", today)
```

- [ ] **Step 4: Implement runner change**

`__init__` gains `alerts=None` (store `self._alerts`). In `run_once`, replace the job loop:

```python
        for job in self._sched.poll(now):
            try:
                self._run_job(job, now)
            except Exception as e:
                logger.error("lifecycle job %s failed: %s", job, e, exc_info=True)
                if self._alerts is not None:
                    self._alerts.send(
                        f"⚠ lifecycle job {job} FAILED: {e} — will retry next poll",
                        key=f"job-fail:{job}:{now.date().isoformat()}")
                continue     # later due jobs still run; this one retries next poll
            self._sched.mark_fired(job, now)
```

- [ ] **Step 5: Fix existing tests that assumed poll() self-marks**

Run: `uv run pytest -q` and update failing scheduler/runner tests to call `mark_fired` where they previously relied on poll-marking (behavior contract change is intentional, per spec V2). Do not weaken assertions about at-most-once across restart — those now assert marking happens via `mark_fired`.

- [ ] **Step 6: Run full suite** — `uv run pytest -q` → green.

- [ ] **Step 7: Commit**

```bash
git add autotrader/scheduler.py autotrader/runner.py tests/
git commit -m "fix(scheduler): mark jobs fired only on success; isolate per-job failures (C1)"
```

---

### Task 4: V3a — Attach trailing stop off the confirmed entry fill (C2)

**Files:**
- Modify: `autotrader/main.py` (`_route_signal`, `_attach_trailing_stop`, new `_confirm_off_book`, refactor `_confirm_hedge_fill` to share it)
- Test: `tests/test_stop_attach_async.py` (new)

**Interfaces:**
- Consumes: Task 1's async SimBroker; existing `_order_working`, `backoff_seconds`, `_hedge_confirm_sleep`.
- Produces: `_attach_trailing_stop(symbol, qty, ref_price, entry_signal_id, entry_ack=None) -> bool`. When `entry_ack` is given and not FILLED, the method polls the open-orders book (bounded, `_hedge_confirm_attempts`, backoff via `_hedge_confirm_sleep`) until the entry leaves the book; only then does it fetch the snapshot and attach. Unconfirmed → returns False with a warning (Task 5's intraday reconcile is the backstop). Shared helper: `_confirm_off_book(ack) -> bool` (extracted from `_confirm_hedge_fill`, which now delegates to it).

- [ ] **Step 1: Write the failing test**

```python
# tests/test_stop_attach_async.py
"""C2: on live, entries fill async — the stop must attach off the confirmed
fill, not the instant snapshot. Uses the V1 async SimBroker rig."""
from autotrader.config import RiskConfig
from autotrader.main import TradeEngine
from autotrader.sim_broker import SimBroker
from autotrader.strategies.breakout import BreakoutParams, BreakoutStrategy


def _cfg(**kw):
    base = dict(trading_env="PAPER", min_confidence=0.5, max_order_notional=1e6,
                max_position_qty=1000, daily_loss_limit=500, max_gross_exposure=1e6,
                allowed_symbols=frozenset({"US.TEST"}), trailing_stop_pct=5.0)
    base.update(kw)
    return RiskConfig(**base)


def _engine(broker, tick_sleeper):
    strat = BreakoutStrategy(BreakoutParams(symbol="US.TEST", stop_loss_pct=0.05,
                                            take_profit_pct=0.10, confidence=0.9))
    return TradeEngine(broker, strat, _cfg(), order_qty=10,
                       audit_path="/tmp/at-test-audit.jsonl",
                       hedge_confirm_sleep=tick_sleeper)


def test_stop_attaches_after_async_entry_fill(tmp_path):
    b = SimBroker({"US.TEST": 100.0}, cash=100000.0, fill_latency_ticks=1,
                  recent_highs={"US.TEST": 90.0})   # price 100 > ref 90 -> breakout BUY
    # The confirm-poll's injected sleep advances the sim market: each backoff
    # tick matures pending fills, exactly like wall time on live.
    eng = _engine(b, tick_sleeper=lambda _s: b.tick_market())
    res = eng.tick()
    assert res.action == "ORDER_PLACED"
    # entry filled during the confirm poll; the stop must now be resting
    stops = [o for o in b.get_open_orders() if o.client_order_id.startswith("at-")]
    assert len(stops) == 1                       # exactly one resting trailing stop
    assert b.get_account().position_qty("US.TEST") == 10


def test_unconfirmed_entry_attaches_no_stop_and_returns_placed(tmp_path):
    b = SimBroker({"US.TEST": 100.0}, cash=100000.0, fill_latency_ticks=99,
                  recent_highs={"US.TEST": 90.0})
    eng = _engine(b, tick_sleeper=lambda _s: None)   # market never advances
    res = eng.tick()
    assert res.action == "ORDER_PLACED"              # entry itself was accepted
    # entry never confirmed -> NO stop submitted (and no mis-directed short)
    working = b.get_open_orders()
    assert len(working) == 1                         # only the unfilled entry rests
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_stop_attach_async.py -v`
Expected: FAIL — first test attaches no stop today (snapshot shows no position while the entry is pending, risk core rejects the stop), so `len(stops) == 1` fails.

- [ ] **Step 3: Implement**

Extract the poll loop from `_confirm_hedge_fill` into a shared helper on `TradeEngine`:

```python
    def _confirm_off_book(self, ack) -> bool:
        """True iff ack's order is confirmed OFF the open-orders book (filled/
        terminal). FILLED at ack (paper) -> True immediately; else bounded poll
        with injected backoff sleep. None (query failed) never counts as off."""
        if ack.state is OrderState.FILLED:
            return True
        first = self._order_working(ack)
        if first is False:
            return True
        for attempt in range(1, self._hedge_confirm_attempts + 1):
            self._hedge_confirm_sleep(backoff_seconds(attempt))
            state = self._order_working(ack)
            if state is False:
                return True
            if state is None:
                logger.warning("fill-confirm: open-orders query failed on "
                               "attempt %d — cannot confirm", attempt)
        return False
```

`_confirm_hedge_fill(ack, req)` becomes a thin wrapper: `confirmed = self._confirm_off_book(ack)`; keep its existing warning log when False (preserving message text used by existing tests).

`_attach_trailing_stop` gains `entry_ack=None` and confirms first (replace the LIVE NOTE paragraph — the fix has landed):

```python
    def _attach_trailing_stop(self, symbol, qty, ref_price, entry_signal_id,
                              entry_ack=None) -> bool:
        # C2 fix: on live a MARKET BUY acks SUBMITTED and fills async. Attach
        # only after the entry is confirmed off the book, so the re-fetched
        # snapshot reflects the position and the risk core approves the stop.
        if entry_ack is not None and not self._confirm_off_book(entry_ack):
            logger.warning("trailing stop for %s NOT attached — entry %s not "
                           "confirmed filled (intraday stop reconcile is the "
                           "backstop)", symbol, entry_ack.client_order_id)
            return False
        snap = self._b.get_account()
        ...  # rest unchanged
```

In `_route_signal`, pass the ack: `self._attach_trailing_stop(signal.symbol, eff_qty, price, signal_id, entry_ack=ack)`. The public `attach_trailing_stop` (StopManager path) keeps `entry_ack=None` — the position already exists there.

- [ ] **Step 4: Run tests** — `uv run pytest tests/test_stop_attach_async.py tests/ -q` → PASS, suite green (existing hedge-confirm tests must still pass; the refactor preserves behavior).

- [ ] **Step 5: Commit**

```bash
git add autotrader/main.py tests/test_stop_attach_async.py
git commit -m "fix(engine): attach trailing stop off confirmed entry fill (C2)"
```

---

### Task 5: V3b — Intraday stop reconcile at RISK_CHECK_MID/LATE and RISK_SWEEP

**Files:**
- Modify: `autotrader/runner.py` (`_run_job`)
- Test: `tests/test_intraday_stop_reconcile.py` (new)

**Interfaces:**
- Consumes: existing `StopManager.reconcile(today)` (already None-safe on failed queries), `EntryGate.halted`.
- Produces: `_run_job` calls `self._stop_manager.reconcile(now.date())` for `RISK_CHECK_MID`, `RISK_CHECK_LATE`, and `RISK_SWEEP` — **after** `apply_risk_check` for the risk jobs and **skipped when the gate is halted** (a flattened book needs no stops). ENTRY_OPEN behavior unchanged.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_intraday_stop_reconcile.py
from datetime import datetime, date

from autotrader.lifecycle import EntryGate
from autotrader.runner import SessionRunner
from autotrader.scheduler import LifecycleScheduler


class _Recorder:
    def __init__(self):
        self.calls = []
    def reconcile(self, today):
        self.calls.append(today)


class _Engine:
    def apply_risk_check(self, now):
        return "OK"
    def tick(self):
        class R: action = "NO_SIGNAL"
        return R()


class _Watch:
    def ensure_healthy(self):
        return True


class _DB:
    def replace_positions(self, p): pass
    def record_fills(self, f): return 0
    def open_trailing_stop_ids(self): return []
    def get_state(self, k): return None
    def set_state(self, k, v): pass
    def record_performance(self, *a, **k): pass


def _runner(sm, gate=None):
    return SessionRunner(engine=_Engine(), broker=None, db=None,
                         gate=gate or EntryGate(enabled=True),
                         scheduler=LifecycleScheduler(), watchdog=_Watch(),
                         clock=None, sleep=lambda s: None, stop_manager=sm)


def test_mid_risk_check_runs_stop_reconcile():
    sm = _Recorder()
    _runner(sm).run_once(datetime(2026, 7, 6, 13, 31))  # PRE_OPEN..RISK_CHECK_MID due
    assert date(2026, 7, 6) in sm.calls


def test_halted_gate_skips_reconcile():
    sm = _Recorder()
    gate = EntryGate(enabled=True)
    gate.halt()
    _runner(sm, gate).run_once(datetime(2026, 7, 6, 13, 31))
    assert sm.calls == []
```

- [ ] **Step 2: Run to verify failure** — `uv run pytest tests/test_intraday_stop_reconcile.py -v` → first test FAILS (no reconcile at MID today).

- [ ] **Step 3: Implement** — in `_run_job`:

```python
        elif job in (RISK_CHECK_MID, RISK_CHECK_LATE):
            self._engine.apply_risk_check(now)
            ...  # existing sync + record_perf unchanged
            # V3b: intraday stop backstop — any position whose entry-time stop
            # attach was unconfirmed (C2) is protected within hours, not next
            # morning. Skipped when halted: a flattened book needs no stops.
            if (self._stop_manager is not None
                    and not (self._gate is not None and self._gate.halted)):
                self._stop_manager.reconcile(now.date())
```

and in the `RISK_SWEEP` branch add the same guarded `self._stop_manager.reconcile(now.date())` after `_record_perf()`.

- [ ] **Step 4: Run tests** — `uv run pytest tests/test_intraday_stop_reconcile.py tests/ -q` → PASS, suite green.

- [ ] **Step 5: Commit**

```bash
git add autotrader/runner.py tests/test_intraday_stop_reconcile.py
git commit -m "feat(runner): intraday stop reconcile at MID/LATE/SWEEP (C2 backstop)"
```

---

### Task 6: V4a — Fail-closed day_pnl (`day_pnl_known`)

**Files:**
- Modify: `autotrader/domain.py` (`AccountSnapshot`), `autotrader/moomoo_broker.py` (`get_account`), `autotrader/risk_check.py`
- Test: `tests/test_day_pnl_fail_closed.py` (new)

**Interfaces:**
- Consumes: `MoomooBroker.get_account` field mapping (`realized_pl` → `today_pnl_value` fallback at moomoo_broker.py:339), `risk_check.evaluate`.
- Produces: `AccountSnapshot` gains `day_pnl_known: bool = True`. When **neither** broker field is present/finite, the snapshot carries `day_pnl=0.0, day_pnl_known=False`. `risk_check.evaluate` returns `RiskAction.GATE` when `day_pnl_known` is False (fail closed: entries blocked, positions and stops kept, no flatten). SimBroker keeps the default True.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_day_pnl_fail_closed.py
from autotrader.config import RiskConfig
from autotrader.domain import AccountSnapshot
from autotrader.risk_check import evaluate, RiskAction


def _cfg():
    return RiskConfig(trading_env="PAPER", min_confidence=0.5,
                      max_order_notional=1e6, max_position_qty=1000,
                      daily_loss_limit=500, max_gross_exposure=1e6,
                      allowed_symbols=frozenset())


def _snap(**kw):
    base = dict(cash=1000.0, total_assets=1000.0, day_pnl=0.0, stale=False)
    base.update(kw)
    return AccountSnapshot(**base)


def test_unknown_day_pnl_gates_entries():
    assert evaluate(_snap(day_pnl_known=False), _cfg()) is RiskAction.GATE


def test_known_zero_pnl_ok():
    assert evaluate(_snap(day_pnl_known=True), _cfg()) is RiskAction.OK


def test_unknown_never_halts_even_when_zero():
    # fail CLOSED means block entries — never flatten off unknown data
    assert evaluate(_snap(day_pnl_known=False), _cfg()) is not RiskAction.HALT
```

- [ ] **Step 2: Run to verify failure** — `uv run pytest tests/test_day_pnl_fail_closed.py -v` → FAIL (`unexpected keyword 'day_pnl_known'`).

- [ ] **Step 3: Implement**

`domain.py` — add after `positions_loaded`:

```python
    day_pnl_known: bool = True   # False = broker returned no P&L field: fail CLOSED
```

`risk_check.py` — first check in `evaluate`:

```python
def evaluate(snapshot: AccountSnapshot, cfg: RiskConfig) -> RiskAction:
    if not snapshot.day_pnl_known:
        # Broker returned no P&L field. Unknown loss must never read as "no
        # loss": block new entries, keep positions + stops, never flatten.
        return RiskAction.GATE
    ...
```

`moomoo_broker.py` `get_account` — replace line 339's default-0 mapping:

```python
        import math
        pnl_raw = self._c.safe_get(acc.iloc[0], "realized_pl", "today_pnl_value",
                                   default=None)
        pnl = self._c.safe_float(pnl_raw) if pnl_raw is not None else 0.0
        day_pnl_known = pnl_raw is not None and math.isfinite(pnl)
        if not day_pnl_known:
            pnl = 0.0
            logger.warning("get_account: no realized_pl/today_pnl_value field — "
                           "day_pnl UNKNOWN (risk check will fail closed)")
```

and thread `day_pnl_known=day_pnl_known` into **both** `AccountSnapshot(...)` constructions in `get_account`. (`import math` goes to the module top, not inline.)

Also in `main.py:apply_risk_check`, log distinctly when gating on unknown:

```python
        if action is RiskAction.GATE:
            if self._gate is not None:
                self._gate.close()
            why = "day_pnl UNKNOWN (fail closed)" if not snap.day_pnl_known \
                else f"pnl={snap.day_pnl:.2f}"
            logger.warning("RISK_CHECK soft breach: entries closed (%s)", why)
```

- [ ] **Step 4: Run tests** — `uv run pytest tests/test_day_pnl_fail_closed.py tests/test_moomoo_broker_offline.py tests/ -q` → PASS.

- [ ] **Step 5: Commit**

```bash
git add autotrader/domain.py autotrader/risk_check.py autotrader/moomoo_broker.py autotrader/main.py tests/test_day_pnl_fail_closed.py
git commit -m "fix(risk): day_pnl fails closed when broker fields absent (V4a)"
```

---

### Task 7: V4b — Unrealized-drawdown entry gate (config + risk_check)

**Files:**
- Modify: `autotrader/config.py`, `autotrader/risk_check.py`
- Test: `tests/test_unrealized_gate.py` (new); extend `tests/test_config.py`

**Interfaces:**
- Consumes: `AccountSnapshot.unrealized_pnl`, `RiskConfig`.
- Produces: `RiskConfig.unrealized_loss_gate: float = 0.0` (env `RISK_UNREALIZED_LOSS_GATE`, positive dollars; `0` disables). `risk_check.evaluate` returns `GATE` when `unrealized_pnl <= -abs(cfg.unrealized_loss_gate)` (checked after HALT/GATE realized checks). **Never HALT on unrealized** — approved design: block entries only.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_unrealized_gate.py
from autotrader.config import RiskConfig
from autotrader.domain import AccountSnapshot
from autotrader.risk_check import evaluate, RiskAction


def _cfg(gate=300.0):
    return RiskConfig(trading_env="PAPER", min_confidence=0.5,
                      max_order_notional=1e6, max_position_qty=1000,
                      daily_loss_limit=500, max_gross_exposure=1e6,
                      allowed_symbols=frozenset(), unrealized_loss_gate=gate)


def _snap(unreal):
    return AccountSnapshot(cash=1000.0, total_assets=1000.0, day_pnl=0.0,
                           stale=False, unrealized_pnl=unreal)


def test_breach_gates():
    assert evaluate(_snap(-301.0), _cfg()) is RiskAction.GATE


def test_breach_never_halts():
    assert evaluate(_snap(-99999.0), _cfg()) is not RiskAction.HALT


def test_zero_config_disables():
    assert evaluate(_snap(-99999.0), _cfg(gate=0.0)) is RiskAction.OK


def test_realized_halt_still_wins():
    snap = AccountSnapshot(cash=0, total_assets=0, day_pnl=-2000.0, stale=False,
                           unrealized_pnl=-301.0)
    assert evaluate(snap, _cfg()) is RiskAction.HALT
```

- [ ] **Step 2: Run to verify failure** — FAIL (`unexpected keyword 'unrealized_loss_gate'`).

- [ ] **Step 3: Implement**

`config.py`: add field after `daily_loss_halt`:

```python
    # Unrealized-drawdown ENTRY BLOCK (V4b): positive dollars; 0 disables. On
    # breach only NEW entries are blocked — never a flatten (open positions
    # exit via their trailing stops). Closes the "open position collapses
    # intraday, realized-only halt never fires" hole.
    unrealized_loss_gate: float = 0.0
```

and in `load_risk_config`: `unrealized_loss_gate=_f("RISK_UNREALIZED_LOSS_GATE", 0.0),`

`risk_check.py` after the existing two checks:

```python
    if (cfg.unrealized_loss_gate > 0
            and snapshot.unrealized_pnl <= -abs(cfg.unrealized_loss_gate)):
        return RiskAction.GATE
```

- [ ] **Step 4: Run tests** — `uv run pytest tests/test_unrealized_gate.py tests/ -q` → PASS.

- [ ] **Step 5: Commit**

```bash
git add autotrader/config.py autotrader/risk_check.py tests/test_unrealized_gate.py
git commit -m "feat(risk): unrealized-drawdown entry gate, never auto-flatten (V4b)"
```

---

### Task 8: V4c — Durable session halt across restart

**Files:**
- Modify: `autotrader/main.py` (`apply_risk_check`, `main()`), `autotrader/lifecycle.py` (new `restore_session_halt`)
- Test: `tests/test_durable_halt.py` (new)

**Interfaces:**
- Consumes: `DB.set_state/get_state` (engine_state table, W4), `EntryGate.halt()`.
- Produces: on HALT, `apply_risk_check` persists `db.set_state(f"halt:{now.date().isoformat()}", reason)`. New `lifecycle.restore_session_halt(gate: EntryGate, db, today: date) -> bool` — halts the gate and returns True when a halt row exists for `today`; `main()` calls it right after constructing the gate. A restart on a halted day therefore starts halted; the next day starts clean (key is date-scoped, no cleanup needed).

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_durable_halt.py
from datetime import date, datetime

from autotrader.db import DB
from autotrader.lifecycle import EntryGate, restore_session_halt


def test_restore_halts_gate_when_todays_halt_persisted(tmp_path):
    db = DB(str(tmp_path / "t.db"))
    db.set_state("halt:2026-07-06", "daily loss halt: pnl=-2000")
    gate = EntryGate(enabled=False)
    assert restore_session_halt(gate, db, date(2026, 7, 6)) is True
    assert gate.halted


def test_no_restore_on_other_day(tmp_path):
    db = DB(str(tmp_path / "t.db"))
    db.set_state("halt:2026-07-05", "yesterday")
    gate = EntryGate(enabled=False)
    assert restore_session_halt(gate, db, date(2026, 7, 6)) is False
    assert not gate.halted


def test_apply_risk_check_persists_halt(tmp_path):
    from autotrader.config import RiskConfig
    from autotrader.main import TradeEngine
    from autotrader.sim_broker import SimBroker
    from autotrader.strategies.breakout import BreakoutParams, BreakoutStrategy

    class _LossBroker(SimBroker):
        def get_account(self):
            snap = super().get_account()
            object.__setattr__(snap, "day_pnl", -5000.0)   # frozen dataclass poke
            return snap

    db = DB(str(tmp_path / "t.db"))
    cfg = RiskConfig(trading_env="PAPER", min_confidence=0.5, max_order_notional=1e6,
                     max_position_qty=1000, daily_loss_limit=500, max_gross_exposure=1e6,
                     allowed_symbols=frozenset(), daily_loss_halt=1000.0)
    strat = BreakoutStrategy(BreakoutParams(symbol="US.TEST", stop_loss_pct=0.05,
                                            take_profit_pct=0.10, confidence=0.9))
    gate = EntryGate(enabled=True)
    eng = TradeEngine(_LossBroker({"US.TEST": 100.0}), strat, cfg, order_qty=1,
                      audit_path=str(tmp_path / "a.jsonl"), db=db, entry_gate=gate)
    assert eng.apply_risk_check(datetime(2026, 7, 6, 13, 30)) == "HALT"
    assert db.get_state("halt:2026-07-06") is not None
```

Implementer note: if `AccountSnapshot` is frozen and `object.__setattr__` proves brittle, build the loss snapshot with a small stub broker instead — the assertion that matters is the persisted `halt:2026-07-06` row.

- [ ] **Step 2: Run to verify failure** — FAIL (`ImportError: cannot import name 'restore_session_halt'`).

- [ ] **Step 3: Implement**

`lifecycle.py`:

```python
def restore_session_halt(gate: EntryGate, db, today) -> bool:
    """V4c: a hard HALT survives a crash/restart. apply_risk_check persists
    halt:<date>; restoring here means a restart on a halted day starts halted
    (the date-scoped key auto-expires — the next session starts clean)."""
    raw = db.get_state(f"halt:{today.isoformat()}")
    if raw:
        gate.halt()
        logger.error("session halt RESTORED from state (%s) — trading stays "
                     "halted for the day", raw)
        return True
    return False
```

`main.py` `apply_risk_check` HALT branch, next to `record_halt`:

```python
            if self._db:
                self._db.record_halt(reason)
                self._db.set_state(f"halt:{now.date().isoformat()}", reason)
```

`main()` after `gate = EntryGate(enabled=False)` and after `db` exists (move gate construction below the DB block, keeping wiring order):

```python
    from autotrader.lifecycle import restore_session_halt
    restore_session_halt(gate, db, __import__("datetime").date.today())
```

(Implementer: use a plain `from datetime import date` import at the top of `main()` — the dunder form above is illustrative only, do not ship it.)

- [ ] **Step 4: Run tests** — `uv run pytest tests/test_durable_halt.py tests/ -q` → PASS.

- [ ] **Step 5: Commit**

```bash
git add autotrader/lifecycle.py autotrader/main.py tests/test_durable_halt.py
git commit -m "fix(risk): hard halt survives restart via engine_state (V4c)"
```

---

### Task 9: V4d — Halt cancels before flattening (liquidation SELLs never self-cancelled)

**Files:**
- Modify: `autotrader/main.py` (`apply_risk_check`)
- Test: `tests/test_halt_flatten_order.py` (new)

**Interfaces:**
- Consumes: Task 1 async SimBroker, existing `_flatten_all`, `cancel_all`.
- Produces: `apply_risk_check` HALT runs `cancel_all()` **before** `_flatten_all(...)`. Nothing in the HALT path cancels orders after the flatten SELLs are submitted, so on live (async fills) the liquidation orders rest and fill instead of being swept.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_halt_flatten_order.py
"""V4d: on live, flatten SELLs fill async — a cancel_all AFTER _flatten_all
would cancel the liquidation itself. Order must be cancel-then-flatten."""
from datetime import datetime

from autotrader.config import RiskConfig
from autotrader.domain import OrderRequest
from autotrader.lifecycle import EntryGate
from autotrader.main import TradeEngine
from autotrader.sim_broker import SimBroker
from autotrader.strategies.breakout import BreakoutParams, BreakoutStrategy


class _LossBroker(SimBroker):
    def get_account(self):
        snap = super().get_account()
        object.__setattr__(snap, "day_pnl", -5000.0)
        return snap


def test_flatten_sells_survive_the_halt_cancel(tmp_path):
    b = _LossBroker({"US.TEST": 100.0}, cash=100000.0, fill_latency_ticks=5)
    # Seed: one held long (sync entry via a second, synchronous broker view is
    # overkill — just poke the position book the way SimBroker itself does).
    from autotrader.domain import Position
    b._positions["US.TEST"] = Position("US.TEST", 10, 90.0)
    # Seed: one stale working order that the halt SHOULD cancel.
    b._fill_latency = 0   # place the resting order synchronously…
    stale = b.place_order(OrderRequest(symbol="US.TEST", side="SELL", qty=10,
                                       order_type="TRAILING_STOP", limit_price=None,
                                       client_order_id="at-stale-stop",
                                       trail_percent=5.0))
    b._fill_latency = 5   # …then restore async mode for the flatten SELLs
    cfg = RiskConfig(trading_env="PAPER", min_confidence=0.5, max_order_notional=1e6,
                     max_position_qty=1000, daily_loss_limit=500,
                     max_gross_exposure=1e6, allowed_symbols=frozenset({"US.TEST"}),
                     daily_loss_halt=1000.0)
    strat = BreakoutStrategy(BreakoutParams(symbol="US.TEST", stop_loss_pct=0.05,
                                            take_profit_pct=0.10, confidence=0.9))
    eng = TradeEngine(b, strat, cfg, order_qty=1,
                      audit_path=str(tmp_path / "a.jsonl"),
                      entry_gate=EntryGate(enabled=True))
    assert eng.apply_risk_check(datetime(2026, 7, 6, 13, 30)) == "HALT"
    working = b.get_open_orders()
    cids = {o.client_order_id for o in working}
    assert "at-stale-stop" not in cids            # stale order was cancelled
    assert len(working) == 1                       # the flatten SELL still rests
    for _ in range(5):
        b.tick_market()
    assert b.get_account().position_qty("US.TEST") == 0   # liquidation completed
```

- [ ] **Step 2: Run to verify failure** — FAIL: today `cancel_all` runs after `_flatten_all`, sweeping the resting flatten SELL (`len(working) == 1` fails / position never flattens).

- [ ] **Step 3: Implement** — reorder in `apply_risk_check`:

```python
        elif action is RiskAction.HALT:
            reason = f"daily loss halt: pnl={snap.day_pnl}"
            round_id = f"halt-{now.date().isoformat()}"
            # V4d: cancel FIRST (clears stops/limits), THEN flatten — the
            # liquidation SELLs must never be swept by our own cancel. On live
            # they fill async and must be left resting.
            try:
                self._b.cancel_all()
            except Exception as e:
                logger.error("RISK_CHECK halt: cancel_all failed: %s", e)
            self._flatten_all(snap, round_id)   # BEFORE halt flag (guard would block)
            if self._gate is not None:
                self._gate.halt()
            ...
```

- [ ] **Step 4: Run tests** — `uv run pytest tests/test_halt_flatten_order.py tests/ -q` → PASS (existing halt tests may assert the old order — update them to the new cancel-then-flatten contract).

- [ ] **Step 5: Commit**

```bash
git add autotrader/main.py tests/
git commit -m "fix(risk): halt cancels before flattening so liquidation SELLs survive (V4d)"
```

---

### Task 10: V5a — Escalation confirms terminal cancel before advancing

**Files:**
- Modify: `autotrader/main.py` (`_cancel_for_escalation` → confirm; `_submit_with_escalation`)
- Test: `tests/test_escalation_cancel_confirm.py` (new)

**Interfaces:**
- Consumes: Task 1 cancel-latency SimBroker, `_order_working`, `_escalation_sleep`, `backoff_seconds`.
- Produces: `_cancel_for_escalation(ack) -> bool` now returns True **only when the order is confirmed off the book** after the cancel request: it polls `_order_working(ack)` up to 3 attempts with `self._escalation_sleep(backoff_seconds(attempt))` between polls. A still-working order, or a None (unknown book), returns False → escalation stops with the current ack (no next stage, no double-fill).

- [ ] **Step 1: Write the failing test**

```python
# tests/test_escalation_cancel_confirm.py
"""V5a: RET_OK from a cancel means 'request accepted', not 'cancelled'. The
next escalation stage must not submit while the prior order can still fill."""
from autotrader.config import RiskConfig
from autotrader.domain import OrderRequest
from autotrader.main import TradeEngine
from autotrader.sim_broker import SimBroker
from autotrader.strategies.breakout import BreakoutParams, BreakoutStrategy


def _cfg():
    return RiskConfig(trading_env="PAPER", min_confidence=0.5, max_order_notional=1e6,
                      max_position_qty=1000, daily_loss_limit=500,
                      max_gross_exposure=1e6, allowed_symbols=frozenset({"US.TEST"}),
                      limit_orders_enabled=True, order_cap_bps=1.0,
                      order_cap_ticks=1.0, escalation_dwell_seconds=1.0)


def test_no_next_stage_while_cancel_unconfirmed(tmp_path):
    # Non-marketable limit rests; cancel takes 99 ticks to mature (never, here);
    # sim market never advances -> the cancel is never confirmed.
    b = SimBroker({"US.TEST": 100.0}, cash=100000.0, spread_bps=50.0,
                  cancel_latency_ticks=99)
    strat = BreakoutStrategy(BreakoutParams(symbol="US.TEST", stop_loss_pct=0.05,
                                            take_profit_pct=0.10, confidence=0.9))
    eng = TradeEngine(b, strat, _cfg(), order_qty=10,
                      audit_path=str(tmp_path / "a.jsonl"),
                      escalation_sleep=lambda _s: None)
    req = OrderRequest(symbol="US.TEST", side="BUY", qty=10, order_type="LIMIT",
                       limit_price=99.0, client_order_id="at-esc-test")
    snap = b.get_account()
    ack, term = eng._submit_with_escalation(req, snap, 100.0)
    # cancel never confirmed -> NO re-peg, NO market stage was submitted
    assert term.client_order_id == "at-esc-test"
    working = b.get_open_orders()
    assert {o.client_order_id for o in working} == {"at-esc-test"}


def test_confirmed_cancel_advances_to_repeg(tmp_path):
    # cancel matures after 1 tick; the escalation sleep advances the market.
    b = SimBroker({"US.TEST": 100.0}, cash=100000.0, spread_bps=50.0,
                  cancel_latency_ticks=1)
    strat = BreakoutStrategy(BreakoutParams(symbol="US.TEST", stop_loss_pct=0.05,
                                            take_profit_pct=0.10, confidence=0.9))
    eng = TradeEngine(b, strat, _cfg(), order_qty=10,
                      audit_path=str(tmp_path / "a.jsonl"),
                      escalation_sleep=lambda _s: b.tick_market())
    req = OrderRequest(symbol="US.TEST", side="BUY", qty=10, order_type="LIMIT",
                       limit_price=99.0, client_order_id="at-esc-test2")
    ack, term = eng._submit_with_escalation(req, b.get_account(), 100.0)
    # original cancelled (confirmed), escalation advanced past it
    assert term.client_order_id != "at-esc-test2"
```

- [ ] **Step 2: Run to verify failure** — first test FAILS today: `_cancel_for_escalation` returns True on the accepted request and the re-peg submits alongside the still-working original (two working orders).

- [ ] **Step 3: Implement**

```python
    def _cancel_for_escalation(self, ack) -> bool:
        """Request the cancel, then CONFIRM the order actually left the book
        before allowing the next stage (V5a): RET_OK acks the REQUEST — an
        in-flight fill between request and effect would double-fill if the
        next stage submitted immediately. Bounded confirm poll; unknown book
        or still-working => False (stop escalating; EOD/reconcile cleans up)."""
        if not ack.broker_order_id:
            return False
        try:
            self._b.cancel_order(ack.broker_order_id)
        except BrokerError as e:
            logger.warning("escalation: cancel %s failed (%s) — NOT advancing",
                           ack.broker_order_id, e)
            return False
        for attempt in range(1, 4):
            self._escalation_sleep(backoff_seconds(attempt))
            working = self._order_working(ack)
            if working is False:
                return True          # confirmed off the book
            if working is None:
                logger.warning("escalation: open-orders unknown while confirming "
                               "cancel of %s — NOT advancing", ack.broker_order_id)
                return False
        logger.warning("escalation: %s still working after cancel request — "
                       "NOT advancing", ack.broker_order_id)
        return False
```

No change needed in `_submit_with_escalation` call sites — they already treat False as "stop".

- [ ] **Step 4: Run tests** — `uv run pytest tests/test_escalation_cancel_confirm.py tests/test_limit_escalation.py tests/ -q` → PASS. Existing escalation tests use the synchronous SimBroker (cancel effective immediately, first confirm poll sees it off-book) so they still pass; update any that stubbed `cancel_order` without book effect.

- [ ] **Step 5: Commit**

```bash
git add autotrader/main.py tests/
git commit -m "fix(engine): escalation confirms terminal cancel before next stage (V5a)"
```

---

### Task 11: V5b+V5c — Unknown order statuses stay working; cap validation

**Files:**
- Modify: `autotrader/moomoo_broker.py` (`get_open_orders`), `autotrader/config.py` (`load_risk_config`)
- Test: `tests/test_unknown_status_conservative.py` (new); extend `tests/test_config.py`

**Interfaces:**
- Consumes: `_STATUS_MAP` (top of moomoo_broker.py), `OrderState.UNKNOWN`.
- Produces: `get_open_orders` includes rows whose mapped state is `OrderState.UNKNOWN` in the returned working set (CLAUDE.md: unrecognized statuses are not successes — an unknown status must keep `_order_working` True, keep hedges unconfirmed, keep escalation from advancing). `load_risk_config` raises `ValueError` when `RISK_LIMIT_ORDERS_ENABLED` is on but both `RISK_ORDER_CAP_BPS` and `RISK_ORDER_CAP_TICKS` are 0.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_unknown_status_conservative.py
"""V5b: an unmapped broker order status must surface as WORKING (UNKNOWN),
never silently drop off the book (which reads as 'filled')."""
# Offline: monkeypatch the row-shaping seam the offline broker tests already use.
# Follow the fixture pattern in tests/test_moomoo_broker_offline.py to build a
# MoomooBroker with a stubbed trade context whose order_list_query returns one
# row with order_status="SOME_NEW_STATUS", then assert:
#
#   orders = broker.get_open_orders()
#   assert len(orders) == 1
#   assert orders[0].state is OrderState.UNKNOWN
```

```python
# append to tests/test_config.py
import pytest


def test_limit_orders_enabled_requires_a_cap(monkeypatch):
    monkeypatch.setenv("RISK_LIMIT_ORDERS_ENABLED", "1")
    monkeypatch.setenv("RISK_ORDER_CAP_BPS", "0")
    monkeypatch.setenv("RISK_ORDER_CAP_TICKS", "0")
    from autotrader.config import load_risk_config
    with pytest.raises(ValueError, match="RISK_ORDER_CAP"):
        load_risk_config()


def test_limit_orders_enabled_with_bps_cap_ok(monkeypatch):
    monkeypatch.setenv("RISK_LIMIT_ORDERS_ENABLED", "1")
    monkeypatch.setenv("RISK_ORDER_CAP_BPS", "10")
    monkeypatch.setenv("RISK_ORDER_CAP_TICKS", "0")
    from autotrader.config import load_risk_config
    assert load_risk_config().limit_orders_enabled
```

Implementer: write the moomoo test concretely against the existing offline stub fixtures in `tests/test_moomoo_broker_offline.py` (they already fake `order_list_query` DataFrames) — the comment block above states the required assertions.

- [ ] **Step 2: Run to verify failure** — config tests FAIL (no ValueError today); status test FAILS (row dropped).

- [ ] **Step 3: Implement**

`moomoo_broker.py` line 322 — include UNKNOWN:

```python
            state = _STATUS_MAP.get(status, OrderState.UNKNOWN)
            # V5b: PENDING/SUBMITTED/PARTIAL are working; an UNMAPPED status is
            # unknown and must be treated as working too — dropping it makes a
            # hedge look filled and lets escalation double-submit.
            if state in (OrderState.PENDING, OrderState.SUBMITTED,
                         OrderState.PARTIAL, OrderState.UNKNOWN):
```

`config.py` in `load_risk_config`, after reading the three limit-order values into locals:

```python
    limit_orders_enabled = _b("RISK_LIMIT_ORDERS_ENABLED", False)
    order_cap_bps = _f("RISK_ORDER_CAP_BPS", 0.0)
    order_cap_ticks = _f("RISK_ORDER_CAP_TICKS", 0.0)
    if limit_orders_enabled and order_cap_bps <= 0 and order_cap_ticks <= 0:
        raise ValueError(
            "RISK_LIMIT_ORDERS_ENABLED=1 requires RISK_ORDER_CAP_BPS or "
            "RISK_ORDER_CAP_TICKS > 0 — zero caps degenerate to at-touch limits")
```

(and pass the locals through to the RiskConfig constructor).

- [ ] **Step 4: Run tests** — `uv run pytest tests/test_unknown_status_conservative.py tests/test_config.py tests/ -q` → PASS.

- [ ] **Step 5: Commit**

```bash
git add autotrader/moomoo_broker.py autotrader/config.py tests/
git commit -m "fix(broker,config): unknown statuses stay working; limit caps validated (V5b/V5c)"
```

---

### Task 12: V6a — Webhook auth hardening (bytes compare, dual secret, bounded audit)

**Files:**
- Modify: `autotrader/signals/webhook.py`
- Test: extend `tests/test_webhook.py`

**Interfaces:**
- Consumes: existing `create_app`, `_auth_failure_reason`, `_audit_auth_failure`.
- Produces: `create_app(inbox_dir, secret, max_body=..., token: Optional[str] = None, audit_max_bytes: int = 10_000_000)` — `token` is the bearer header value (independent of the HMAC `secret`); when None it falls back to `secret` (back-compat, logged warning at app creation). `_auth_failure_reason(secret, token_secret, raw, token, signature)` compares **bytes** (`.encode("utf-8", "replace")`) so non-ASCII headers yield an audited 401, never a 500. `_audit_auth_failure` truncates `user_agent`/`forwarded_for` to 256 chars, skips writing when the audit file exceeds `audit_max_bytes`, and throttles to at most 10 records per `remote_addr` per 60-second window (in-memory). `run()` reads `AUTOTRADER_WEBHOOK_TOKEN` (optional) alongside `AUTOTRADER_WEBHOOK_SECRET`.

- [ ] **Step 1: Write the failing tests** (append to `tests/test_webhook.py`, reusing its existing app/signing fixtures — it already builds `create_app` clients and computes `sha256=` signatures)

```python
def test_non_ascii_token_is_401_not_500(client_factory):
    app, inbox = client_factory(secret="s3cret")
    c = app.test_client()
    r = c.post("/webhook/sweep", data=b"{}",
               headers={"X-Webhook-Token": "café", "X-Webhook-Signature": "x"})
    assert r.status_code == 401
    audit = (inbox / "audit" / "webhook-auth.jsonl")
    assert audit.exists() and "token_mismatch" in audit.read_text()


def test_dual_secret_token_alone_cannot_forge(client_factory, sign):
    app, inbox = client_factory(secret="signing-key", token="bearer-token")
    c = app.test_client()
    body = b'{"routine_id":"r1","timestamp":"2026-07-06T14:00:00Z","signal_changes":[]}'
    # correct bearer token, but signature made with the TOKEN not the signing key
    r = c.post("/webhook/sweep", data=body, headers={
        "X-Webhook-Token": "bearer-token",
        "X-Webhook-Signature": sign(body, key="bearer-token")})
    assert r.status_code == 401
    r2 = c.post("/webhook/sweep", data=body, headers={
        "X-Webhook-Token": "bearer-token",
        "X-Webhook-Signature": sign(body, key="signing-key")})
    assert r2.status_code == 202


def test_audit_writes_are_bounded(client_factory):
    app, inbox = client_factory(secret="s3cret")
    c = app.test_client()
    for _ in range(50):   # anonymous hammering from one address
        c.post("/webhook/sweep", data=b"x", headers={"User-Agent": "A" * 10000})
    lines = (inbox / "audit" / "webhook-auth.jsonl").read_text().splitlines()
    assert len(lines) <= 10                      # per-IP throttle
    import json
    assert all(len(json.loads(l).get("user_agent") or "") <= 256 for l in lines)
```

Implementer: `client_factory`/`sign` describe the shape — adapt to the fixture names actually present in `tests/test_webhook.py` (or add small local helpers mirroring its existing setup) rather than inventing new global fixtures.

- [ ] **Step 2: Run to verify failure** — non-ASCII test FAILS with 500 today (`TypeError` from `hmac.compare_digest`); dual-secret factory kwarg FAILS.

- [ ] **Step 3: Implement**

`_auth_failure_reason` — bytes + dual secret:

```python
def _auth_failure_reason(secret: str, token_secret: str, raw: bytes,
                         token: Optional[str], signature: Optional[str]) -> Optional[str]:
    if not secret or not token_secret:
        return "server_secret_unset"
    if not token:
        return "missing_token"
    if not signature:
        return "missing_signature"
    # bytes compare: Flask decodes headers latin-1; a non-ASCII header must be
    # an audited 401, never an unaudited TypeError->500 (V6a).
    if not hmac.compare_digest(token.encode("utf-8", "replace"),
                               token_secret.encode("utf-8")):
        return "token_mismatch"
    expected = "sha256=" + hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(signature.encode("utf-8", "replace"),
                               expected.encode("utf-8")):
        return "bad_signature"
    return None
```

`create_app(inbox_dir, secret, max_body=_MAX_BODY_DEFAULT, token=None, audit_max_bytes=10_000_000)`: resolve `token_secret = token or secret` (log a warning when falling back: "single-secret mode: token == signing key — set AUTOTRADER_WEBHOOK_TOKEN"). Thread `token_secret` into the `_auth_failure_reason` call. Add a module-level throttle:

```python
_AUDIT_TRUNC = 256
_AUDIT_WINDOW_S = 60.0
_AUDIT_MAX_PER_ADDR = 10


class _AuditThrottle:
    def __init__(self):
        self._hits = {}   # addr -> [monotonic timestamps]
    def allow(self, addr: str, now: float) -> bool:
        hits = [t for t in self._hits.get(addr, []) if now - t < _AUDIT_WINDOW_S]
        if len(hits) >= _AUDIT_MAX_PER_ADDR:
            self._hits[addr] = hits
            return False
        hits.append(now)
        self._hits[addr] = hits
        return True
```

In the 401 branch: truncate `user_agent`/`forwarded_for` with `(value or "")[:_AUDIT_TRUNC] or None`, check `throttle.allow(request.remote_addr or "?", time.monotonic())`, and inside `_audit_auth_failure` skip the write when `(audit_dir / _AUTH_AUDIT_FILE)` exists and `.stat().st_size > audit_max_bytes` (pass the limit through). The 401 HTTP response itself is unchanged — only the disk write is bounded.

`run()`: `token = os.getenv("AUTOTRADER_WEBHOOK_TOKEN") or None`, pass to `create_app`.

- [ ] **Step 4: Run tests** — `uv run pytest tests/test_webhook.py tests/ -q` → PASS (update existing `_auth_failure_reason`/`_verify` call sites and their direct tests to the new signature).

- [ ] **Step 5: Commit**

```bash
git add autotrader/signals/webhook.py tests/test_webhook.py
git commit -m "fix(webhook): bytes-safe auth, independent bearer token, bounded 401 audit (V6a)"
```

---

### Task 13: V6b — Replay protection: freshness window + routine_id dedup + signal TTL (C3)

**Files:**
- Modify: `autotrader/signals/webhook.py`, `autotrader/signals/inbox.py`, `autotrader/main.py` (inbox wiring)
- Test: `tests/test_replay_protection.py` (new)

**Interfaces:**
- Consumes: `RoutineSignalPayload.timestamp` (datetime) / `.routine_id`, `DB.get_state/set_state`.
- Produces:
  - `create_app(..., freshness_minutes: float = 15.0, now_fn=None)`: after validation (both direct and coerced paths), reject with **400 `{"error": "stale_timestamp"}`** and quarantine when `|now_utc − payload.timestamp| > freshness_minutes` (naive timestamps are treated as UTC; `freshness_minutes=0` disables; `now_fn` injectable for tests). Env: `AUTOTRADER_WEBHOOK_FRESHNESS_MIN` in `run()`.
  - `SignalInbox(..., seen_get=None, seen_set=None, ttl_hours: float = 24.0, now_fn=None)`: `poll()` skips (→ `processed/`, log "duplicate routine_id") any payload whose `seen_get(f"routine:{routine_id}")` is truthy, and quarantines (→ `rejected/`) payloads older than `ttl_hours` versus `now_fn()`; on successful processing calls `seen_set(f"routine:{routine_id}", <today iso>)`. Defense in depth: this covers the file-drop adapter path the webhook never sees.
  - `main()` wires `seen_get=db.get_state, seen_set=db.set_state`, `ttl_hours=float(os.getenv("AUTOTRADER_SIGNAL_TTL_HOURS", "24"))`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_replay_protection.py
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from autotrader.signals.inbox import SignalInbox, atomic_write_bytes


def _payload(routine_id="r1", ts=None):
    ts = ts or datetime(2026, 7, 6, 14, 0, tzinfo=timezone.utc)
    return json.dumps({
        "routine_id": routine_id, "timestamp": ts.isoformat(),
        "signal_changes": [{"ticker": "US.TEST", "direction": "UP",
                            "points_delta": 5, "driver": "test"}],
    }).encode()


def test_duplicate_routine_id_processed_once(tmp_path):
    seen = {}
    now = datetime(2026, 7, 6, 14, 5, tzinfo=timezone.utc)
    inbox = SignalInbox(str(tmp_path), seen_get=seen.get, seen_set=seen.__setitem__,
                        now_fn=lambda: now)
    atomic_write_bytes(Path(tmp_path), _payload())
    assert len(inbox.poll()) == 1
    assert seen.get("routine:r1")
    atomic_write_bytes(Path(tmp_path), _payload())      # replay, same routine_id
    assert inbox.poll() == []                           # deduped, no signals


def test_stale_payload_quarantined(tmp_path):
    now = datetime(2026, 7, 8, 14, 0, tzinfo=timezone.utc)   # 2 days later
    inbox = SignalInbox(str(tmp_path), ttl_hours=24.0, now_fn=lambda: now)
    atomic_write_bytes(Path(tmp_path), _payload())
    assert inbox.poll() == []
    assert list((Path(tmp_path) / "rejected").iterdir())     # preserved, not routed


def test_webhook_rejects_outside_freshness_window():
    from autotrader.signals.webhook import create_app
    import hashlib, hmac as hmac_mod, tempfile
    secret = "s3cret"
    with tempfile.TemporaryDirectory() as d:
        now = datetime(2026, 7, 6, 15, 0, tzinfo=timezone.utc)
        app = create_app(d, secret, freshness_minutes=15.0, now_fn=lambda: now)
        c = app.test_client()
        body = _payload(ts=datetime(2026, 7, 6, 14, 0, tzinfo=timezone.utc))  # 60 min old
        sig = "sha256=" + hmac_mod.new(secret.encode(), body, hashlib.sha256).hexdigest()
        r = c.post("/webhook/sweep", data=body,
                   headers={"X-Webhook-Token": secret, "X-Webhook-Signature": sig})
        assert r.status_code == 400
        assert r.get_json()["error"] == "stale_timestamp"
        assert not [p for p in Path(d).glob("*.json")]        # nothing enqueued
```

- [ ] **Step 2: Run to verify failure** — FAIL (unexpected kwargs; replay currently routes twice).

- [ ] **Step 3: Implement webhook freshness**

Helper + check in `create_app` (applied to both the direct and coerced validation paths, before `_atomic_enqueue`):

```python
def _is_stale(payload_ts, now, freshness_minutes: float) -> bool:
    if freshness_minutes <= 0:
        return False
    ts = payload_ts if payload_ts.tzinfo else payload_ts.replace(tzinfo=timezone.utc)
    return abs((now - ts).total_seconds()) > freshness_minutes * 60.0
```

```python
        if _is_stale(payload.timestamp, now_fn(), freshness_minutes):
            qpath = _quarantine(inbox, raw)
            logger.warning("webhook payload stale (ts=%s), quarantined %s",
                           payload.timestamp.isoformat(), qpath.name)
            return jsonify({"error": "stale_timestamp",
                            "routine_id": payload.routine_id}), 400
```

`create_app` signature gains `freshness_minutes: float = 15.0, now_fn=None` with `now_fn = now_fn or (lambda: datetime.now(timezone.utc))`. `run()` reads `AUTOTRADER_WEBHOOK_FRESHNESS_MIN` (default `"15"`).

- [ ] **Step 4: Implement inbox dedup + TTL**

`SignalInbox.__init__` gains `seen_get=None, seen_set=None, ttl_hours: float = 24.0, now_fn=None` (default `now_fn`: `lambda: datetime.now(timezone.utc)`). In `poll()` after validation:

```python
                key = f"routine:{payload.routine_id}"
                if self._seen_get is not None and self._seen_get(key):
                    logger.warning("duplicate routine_id %s — skipped (replay "
                                   "protection)", payload.routine_id)
                    path.replace(self._processed / path.name)
                    continue
                ts = payload.timestamp if payload.timestamp.tzinfo else \
                    payload.timestamp.replace(tzinfo=timezone.utc)
                if self._ttl_hours > 0 and \
                        (self._now() - ts).total_seconds() > self._ttl_hours * 3600.0:
                    logger.warning("signal payload %s older than TTL (%s) — "
                                   "quarantined, not routed", path.name, payload.timestamp)
                    path.replace(self._rejected / path.name)
                    continue
```

and after the existing `signals.extend(...)` + move-to-processed succeeds:

```python
            if self._seen_get is not None and self._seen_set is not None:
                self._seen_set(key, self._now().date().isoformat())
```

`main()` inbox wiring:

```python
        inbox = SignalInbox(os.path.expanduser(inbox_dir),
                            on_targets=db.upsert_target_weights,
                            seen_get=db.get_state, seen_set=db.set_state,
                            ttl_hours=float(os.getenv("AUTOTRADER_SIGNAL_TTL_HOURS", "24")))
```

- [ ] **Step 5: Run tests** — `uv run pytest tests/test_replay_protection.py tests/test_webhook.py tests/ -q` → PASS.

- [ ] **Step 6: Commit**

```bash
git add autotrader/signals/webhook.py autotrader/signals/inbox.py autotrader/main.py tests/test_replay_protection.py
git commit -m "fix(signals): replay protection — freshness window, routine_id dedup, TTL (C3)"
```

---

### Task 14: V6c — Arrival-ordered inbox + targets ingestion safety

**Files:**
- Modify: `autotrader/signals/inbox.py`, `autotrader/signals/webhook.py`
- Test: `tests/test_inbox_ordering_targets.py` (new)

**Interfaces:**
- Consumes: `atomic_write_bytes`, `_atomic_write` (webhook), `SignalInbox.poll`, `on_targets` callback.
- Produces:
  - Both writers name files `<epoch_ns 020d>-drop-<uuid>.json` / `<epoch_ns 020d>-wh-<uuid>.json`; `poll()`'s existing `sorted()` therefore processes in **arrival order** (old unprefixed names sort after digits-first names only within the same poll batch — acceptable one-time transition; note it in the commit).
  - `on_targets` receives a **clamped** as-of date: `min(payload.timestamp.date(), now_fn().date()).isoformat()` — a future-dated payload can no longer become an unbeatable MAX(as_of_date) (log a warning when clamped).
  - The `on_targets` call is wrapped in `try/except Exception`: a failing targets sink (e.g. `sqlite3.OperationalError`) logs the error and **still routes the payload's signals and moves the file** — targets are advisory; one bad sink must not wedge all signal flow.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_inbox_ordering_targets.py
import json
from datetime import datetime, timezone
from pathlib import Path

from autotrader.signals.inbox import SignalInbox, atomic_write_bytes


def _payload(routine_id, ticker="US.TEST", direction="UP", ts="2026-07-06T14:00:00Z",
             targets=None):
    d = {"routine_id": routine_id, "timestamp": ts,
         "signal_changes": [{"ticker": ticker, "direction": direction,
                             "points_delta": 5, "driver": "t"}]}
    if targets is not None:
        d["portfolio_targets"] = targets
    return json.dumps(d).encode()


def test_files_process_in_arrival_order(tmp_path):
    inbox = SignalInbox(str(tmp_path))
    atomic_write_bytes(Path(tmp_path), _payload("r1", direction="UP"))
    atomic_write_bytes(Path(tmp_path), _payload("r2", direction="DOWN"))
    sigs = inbox.poll()
    assert [s.direction for s in sigs] == ["BUY", "SELL"]   # arrival order, always


def test_future_dated_targets_clamped_to_today(tmp_path):
    recorded = []
    now = datetime(2026, 7, 6, 14, 0, tzinfo=timezone.utc)
    inbox = SignalInbox(str(tmp_path), now_fn=lambda: now,
                        on_targets=lambda d, t: recorded.append(d))
    atomic_write_bytes(Path(tmp_path), _payload(
        "r3", ts="2027-01-01T14:00:00Z",
        targets=[{"symbol": "US.TEST", "score": 1.0}]))
    inbox.poll()
    assert recorded == ["2026-07-06"]          # clamped, not 2027-01-01


def test_failing_targets_sink_does_not_block_signals(tmp_path):
    def boom(d, t):
        import sqlite3
        raise sqlite3.OperationalError("database is locked")
    inbox = SignalInbox(str(tmp_path), on_targets=boom)
    atomic_write_bytes(Path(tmp_path), _payload(
        "r4", targets=[{"symbol": "US.TEST", "score": 1.0}]))
    sigs = inbox.poll()
    assert len(sigs) == 1                       # signals still routed
    assert not list(Path(tmp_path).glob("*.json"))   # file moved out of inbox
```

Note: `test_future_dated_targets_clamped_to_today` interacts with Task 13's TTL (a 2027 timestamp vs 2026 now is "in the future", not older-than-TTL — the TTL check uses `(now - ts) > ttl`, a negative delta passes). Keep it that way; freshness at the webhook is the future-guard for the wh- path.

- [ ] **Step 2: Run to verify failure** — ordering test flaky/failing on uuid ordering; clamp + sink tests FAIL.

- [ ] **Step 3: Implement**

`inbox.atomic_write_bytes` final name: `final = inbox_dir / f"{time.time_ns():020d}-drop-{uuid.uuid4().hex}.json"` (add `import time`). `webhook._atomic_write` final name: `final = dest_dir / f"{time.time_ns():020d}-{prefix}{uuid.uuid4().hex}.json"` (add `import time`).

`SignalInbox.poll()` targets block becomes:

```python
                if payload.portfolio_targets and self._on_targets is not None:
                    as_of = min(payload.timestamp.date(), self._now().date())
                    if as_of != payload.timestamp.date():
                        logger.warning("targets payload %s dated in the FUTURE "
                                       "(%s) — clamped to %s", path.name,
                                       payload.timestamp.date(), as_of)
                    try:
                        self._on_targets(
                            as_of.isoformat(),
                            [(t.symbol.upper(), float(t.score))
                             for t in payload.portfolio_targets])
                    except Exception as e:   # targets are ADVISORY: a failing
                        # sink (sqlite locked/full) must not wedge signal flow.
                        logger.error("targets ingestion failed for %s: %s — "
                                     "signals still routed", path.name, e)
```

- [ ] **Step 4: Run tests** — `uv run pytest tests/test_inbox_ordering_targets.py tests/test_inbox_targets.py tests/ -q` → PASS (update existing inbox tests that assert exact `drop-*` filenames).

- [ ] **Step 5: Commit**

```bash
git add autotrader/signals/inbox.py autotrader/signals/webhook.py tests/
git commit -m "fix(signals): arrival-ordered inbox; clamp future targets; advisory targets sink (V6c)"
```

---

### Task 15: V7 — Options chain delta safety + collar structure validation (C4)

**Files:**
- Modify: `autotrader/options/chain.py` (`select_contract`), `autotrader/moomoo_broker.py` (chain row mapping, ~lines 241–247), `autotrader/options/planner.py` (`_validate_structure` collar exemption, ~lines 41–45)
- Test: `tests/test_chain_delta_safety.py` (new); extend `tests/test_options_planner.py`

**Interfaces:**
- Consumes: `OptionQuote`, `select_contract`, planner's `_validate_structure`.
- Produces: `select_contract` candidates additionally require `math.isfinite(q.delta) and q.delta != 0`. The broker chain mapping reads delta with `default=None` and **skips the row** when absent/NaN/zero (log at debug with the code). COLLAR passes through `_validate_structure` like other multi-leg structures, with the rule: protective put strike must be **strictly below** the covered call strike; violations return the planner's existing skip/invalid result (read `planner.py` to reuse the exact skip mechanism other structures use — `SKIP_NO_CONTRACT`-style result, not an exception).

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_chain_delta_safety.py
"""C4: degraded greeks must SKIP, never select. An all-zero-delta chain makes
every candidate 'equidistant' and the tie-break sells the deepest ITM call."""
import math
from datetime import date

from autotrader.options.chain import OptionQuote, select_contract


def _q(strike, delta, premium=1.0, expiry=date(2026, 8, 7)):
    return OptionQuote(code=f"TESTC{int(strike)}", underlying="US.TEST",
                       expiry=expiry, strike=strike, right="CALL",
                       delta=delta, premium=premium)


ASOF = date(2026, 7, 6)


def test_all_zero_delta_chain_selects_nothing():
    chain = [_q(50, 0.0), _q(100, 0.0), _q(150, 0.0)]
    assert select_contract(chain, "CALL", 0.30, 20, 60, ASOF) is None


def test_nan_delta_row_never_wins_regardless_of_order():
    good = _q(105, 0.30)
    nan = _q(50, float("nan"))
    for chain in ([nan, good], [good, nan]):
        pick = select_contract(chain, "CALL", 0.30, 20, 60, ASOF)
        assert pick is good


def test_valid_chain_still_selects_closest_delta():
    chain = [_q(95, 0.55), _q(105, 0.31), _q(115, 0.18)]
    assert select_contract(chain, "CALL", 0.30, 20, 60, ASOF).strike == 105
```

Extend `tests/test_options_planner.py` with a collar-structure case following that file's existing plan-builder fixtures: a chain rigged so the selected put strike ends up **above** the call strike must produce the planner's skip result (not an `OverlayPlan`). Use the same fixture style as the file's existing `_validate_structure` tests for spreads.

- [ ] **Step 2: Run to verify failure** — `uv run pytest tests/test_chain_delta_safety.py -v` → FAIL (zero-delta chain currently selects strike 50).

- [ ] **Step 3: Implement**

`chain.py` — add `import math`; candidate filter gains:

```python
        if q.right == right and q.premium > 0
        and math.isfinite(q.delta) and q.delta != 0        # C4: no greeks -> no pick
        and dte_min <= (q.expiry - asof).days <= dte_max
```

`moomoo_broker.py` chain mapping (~241): read with `default=None` and skip:

```python
                delta_raw = self._c.safe_get(row, "option_delta", "delta", default=None)
                delta = self._c.safe_float(delta_raw) if delta_raw is not None else 0.0
                if delta_raw is None or not math.isfinite(delta) or delta == 0:
                    # C4: a chain row without a usable delta is unusable for
                    # delta-target selection — drop it so SKIP_NO_CONTRACT fires
                    # instead of an arbitrary (deepest-ITM) pick.
                    logger.debug("chain row %s dropped: delta missing/NaN/0", code)
                    continue
                if strike <= 0 or mid <= 0:
                    continue
```

(`import math` at module top.)

`planner.py`: remove COLLAR from the `_validate_structure` exemption and add the collar rule — put strike strictly below call strike; on violation return the same skip result other invalid structures return (mirror the neighboring spread checks; do not raise).

- [ ] **Step 4: Run tests** — `uv run pytest tests/test_chain_delta_safety.py tests/test_options_planner.py tests/test_options_chain.py tests/ -q` → PASS.

- [ ] **Step 5: Commit**

```bash
git add autotrader/options/chain.py autotrader/options/planner.py autotrader/moomoo_broker.py tests/
git commit -m "fix(options): missing/NaN/zero delta skips selection; collar structure validated (C4)"
```

---

### Task 16: V11a — Account-ownership config + owned-fill plumbing

**Files:**
- Modify: `autotrader/config.py`, `autotrader/domain.py` (`Fill`), `autotrader/sim_broker.py`, `autotrader/moomoo_broker.py` (fills mapping), `autotrader/db.py` (new `owned_symbols`)
- Test: `tests/test_account_ownership_config.py` (new)

**Interfaces:**
- Consumes: existing `Fill`, `load_risk_config`, `_fills_from_orders` (moomoo_broker; find it — it builds Fills from the order list on paper), the `at-` cid prefix (`OrderRouter.make_client_order_id` → `"at-" + sha1[:16]`).
- Produces:
  - `RiskConfig.account_ownership: str = "SOLE"` (env `RISK_ACCOUNT_OWNERSHIP`, values `SOLE`|`SHARED`; anything else → ValueError). **`LIVE` + `SHARED` raises ValueError** ("live money never runs with scoped-down sweeps").
  - `Fill` gains `client_order_id: Optional[str] = None`. SimBroker populates it with `req.client_order_id` at every fill site (sync, async `tick_market`). MoomooBroker populates it from the order/deal `remark` field in `_fills_from_orders` and the live deal path (empty remark → None).
  - `DB.owned_symbols() -> set` — `SELECT DISTINCT symbol FROM trades` (every row in `trades` was written by AutoTrader's own order path; this is the persistent ownership registry Task 17 scopes by).

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_account_ownership_config.py
import pytest


def test_default_is_sole(monkeypatch):
    monkeypatch.delenv("RISK_ACCOUNT_OWNERSHIP", raising=False)
    from autotrader.config import load_risk_config
    assert load_risk_config().account_ownership == "SOLE"


def test_shared_paper_ok(monkeypatch):
    monkeypatch.setenv("RISK_ACCOUNT_OWNERSHIP", "shared")
    from autotrader.config import load_risk_config
    assert load_risk_config().account_ownership == "SHARED"


def test_live_plus_shared_refused(monkeypatch):
    monkeypatch.setenv("RISK_TRADING_ENV", "LIVE")
    monkeypatch.setenv("RISK_ACCOUNT_OWNERSHIP", "SHARED")
    from autotrader.config import load_risk_config
    with pytest.raises(ValueError, match="SHARED"):
        load_risk_config()


def test_bogus_value_refused(monkeypatch):
    monkeypatch.setenv("RISK_ACCOUNT_OWNERSHIP", "MAYBE")
    from autotrader.config import load_risk_config
    with pytest.raises(ValueError):
        load_risk_config()


def test_sim_fills_carry_client_order_id():
    from autotrader.domain import OrderRequest
    from autotrader.sim_broker import SimBroker
    b = SimBroker({"US.TEST": 100.0})
    b.place_order(OrderRequest(symbol="US.TEST", side="BUY", qty=1,
                               order_type="MARKET", limit_price=None,
                               client_order_id="at-abc"))
    assert b.reconcile_fills(None)[0].client_order_id == "at-abc"


def test_db_owned_symbols(tmp_path):
    from autotrader.db import DB
    db = DB(str(tmp_path / "t.db"))
    db.record_trade(client_order_id="at-1", symbol="US.MARA", side="BUY", qty=1,
                    order_type="MARKET", limit_price=None,
                    broker_order_id="b1", state="FILLED")
    assert db.owned_symbols() == {"US.MARA"}
```

- [ ] **Step 2: Run to verify failure** — FAIL (missing field / attribute / method).

- [ ] **Step 3: Implement**

`config.py` field + loader:

```python
    # V11: account sovereignty. SOLE = today's behavior (sweep/flatten/report
    # the whole account). SHARED = every account-wide operation scopes to
    # AutoTrader's own tracked book (SNP-bot coexistence on the shared paper
    # account). LIVE+SHARED is structurally refused.
    account_ownership: str = "SOLE"
```

```python
    ownership = os.getenv("RISK_ACCOUNT_OWNERSHIP", "SOLE").strip().upper()
    if ownership not in ("SOLE", "SHARED"):
        raise ValueError(f"RISK_ACCOUNT_OWNERSHIP must be SOLE or SHARED, got {ownership!r}")
    if env == "LIVE" and ownership == "SHARED":
        raise ValueError("RISK_TRADING_ENV=LIVE requires RISK_ACCOUNT_OWNERSHIP=SOLE — "
                         "live money never runs with scoped-down safety sweeps")
```

`domain.py` `Fill`: add `client_order_id: Optional[str] = None` after `ts`.

`sim_broker.py`: both `Fill(...)` constructions (sync path and Task 1's `tick_market`) gain `client_order_id=req.client_order_id`.

`moomoo_broker.py`: locate `_fills_from_orders` (paper fills from order list) and the live deal-feed mapping in `reconcile_fills`; populate `client_order_id=str(remark) or None` from each row's `remark` field via `safe_get(row, "remark", default="")` (empty → None).

`db.py`:

```python
    def owned_symbols(self) -> set:
        """Symbols AutoTrader itself has ever traded (trades is written only by
        our order path) — the ownership registry for SHARED-mode scoping (V11)."""
        rows = self._conn.execute("SELECT DISTINCT symbol FROM trades").fetchall()
        return {r[0] for r in rows}
```

- [ ] **Step 4: Run tests** — `uv run pytest tests/test_account_ownership_config.py tests/ -q` → PASS.

- [ ] **Step 5: Commit**

```bash
git add autotrader/config.py autotrader/domain.py autotrader/sim_broker.py autotrader/moomoo_broker.py autotrader/db.py tests/test_account_ownership_config.py
git commit -m "feat(config,domain): RISK_ACCOUNT_OWNERSHIP + owned-fill plumbing (V11a)"
```

---

### Task 17: V11b — SHARED-mode scoped operations (C6)

**Files:**
- Modify: `autotrader/stops.py`, `autotrader/lifecycle.py` (new `cancel_tracked_orders`; `ground_truth_sync` filter), `autotrader/main.py` (new `TradeEngine.cancel_working_orders`; `_flatten_all`; `shutdown`; `apply_risk_check`), `autotrader/runner.py` (EOD job uses the engine method)
- Test: `tests/test_shared_ownership_scoping.py` (new)

**Interfaces:**
- Consumes: Task 16 (`cfg.account_ownership`, `Fill.client_order_id`, `db.owned_symbols`), `db.get_trade_by_broker_order_id`.
- Produces (all behavior identical to today when `account_ownership == "SOLE"`):
  - `lifecycle.cancel_tracked_orders(broker, db) -> int`: cancels only open orders whose `broker_order_id` exists in the trades projection; returns count; None book → logged no-op returning 0.
  - `TradeEngine.cancel_working_orders() -> None`: dispatches to `broker.cancel_all()` (SOLE) or `cancel_tracked_orders(broker, db)` (SHARED). Used by `shutdown()`, `apply_risk_check` HALT, and the runner's `EOD_CANCEL_ORDERS` job (runner calls `self._engine.cancel_working_orders()` instead of `self._broker.cancel_all()`).
  - `StopManager.reconcile` in SHARED: orphan pass leaves foreign orders (no trades row) untouched with an info log; attach pass covers only `held ∩ db.owned_symbols()`.
  - `ground_truth_sync(broker, db, since=None, owned_only=False)`: with `owned_only=True`, fills are filtered to `f.client_order_id and f.client_order_id.startswith("at-")` and `replace_positions` receives only positions whose symbol is in `db.owned_symbols() ∪ {owned-fill symbols}`. Runner passes `owned_only=(cfg.account_ownership == "SHARED")` — thread `cfg` (or the boolean) into `SessionRunner.__init__` as `owned_only: bool = False` and use it at every `ground_truth_sync` call site.
  - `TradeEngine._flatten_all` in SHARED: skips positions whose symbol is not in `db.owned_symbols()` (log each skip: "foreign position left alone").

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_shared_ownership_scoping.py
"""C6: in SHARED mode AutoTrader must never cancel, stop-manage, flatten, or
ingest the SNP bot's orders/positions/fills."""
from datetime import date

from autotrader.config import RiskConfig
from autotrader.db import DB
from autotrader.domain import Fill, OrderRequest, Position
from autotrader.lifecycle import cancel_tracked_orders, ground_truth_sync
from autotrader.sim_broker import SimBroker
from autotrader.stops import StopManager


def _cfg(ownership="SHARED"):
    return RiskConfig(trading_env="PAPER", min_confidence=0.5, max_order_notional=1e6,
                      max_position_qty=1000, daily_loss_limit=500,
                      max_gross_exposure=1e6, allowed_symbols=frozenset(),
                      trailing_stop_pct=5.0, account_ownership=ownership)


def _seed_foreign_order(broker, cid="snp-order-1"):
    return broker.place_order(OrderRequest(symbol="US.SNP", side="BUY", qty=5,
                                           order_type="TRAILING_STOP",  # rests
                                           limit_price=None, client_order_id=cid,
                                           trail_percent=5.0))


def test_cancel_tracked_leaves_foreign_orders(tmp_path):
    db = DB(str(tmp_path / "t.db"))
    b = SimBroker({"US.SNP": 50.0, "US.MARA": 20.0})
    foreign = _seed_foreign_order(b)
    mine = b.place_order(OrderRequest(symbol="US.MARA", side="SELL", qty=1,
                                      order_type="TRAILING_STOP", limit_price=None,
                                      client_order_id="at-mine", trail_percent=5.0))
    db.record_trade(client_order_id="at-mine", symbol="US.MARA", side="SELL", qty=1,
                    order_type="TRAILING_STOP", limit_price=None,
                    broker_order_id=mine.broker_order_id, state="SUBMITTED")
    assert cancel_tracked_orders(b, db) == 1
    remaining = {o.broker_order_id for o in b.get_open_orders()}
    assert foreign.broker_order_id in remaining          # SNP order untouched
    assert mine.broker_order_id not in remaining


def test_stop_reconcile_shared_ignores_foreign(tmp_path):
    db = DB(str(tmp_path / "t.db"))
    b = SimBroker({"US.SNP": 50.0, "US.MARA": 20.0})
    foreign = _seed_foreign_order(b)
    b._positions["US.SNP"] = Position("US.SNP", 5, 48.0)   # SNP's position

    class _Eng:
        attached = []
        def attach_trailing_stop(self, symbol, qty, price, tag):
            self.attached.append(symbol)
            return True

    sm = StopManager(_Eng(), b, db, _cfg("SHARED"))
    sm.reconcile(date(2026, 7, 6))
    assert foreign.broker_order_id in {o.broker_order_id for o in b.get_open_orders()}
    assert "US.SNP" not in _Eng.attached                 # no stop on SNP's position


def test_ground_truth_sync_owned_only_filters_foreign_fills(tmp_path):
    db = DB(str(tmp_path / "t.db"))
    b = SimBroker({"US.MARA": 20.0})
    b._fills.append(Fill(fill_id="f-snp", symbol="US.SNP", side="BUY", qty=5,
                         price=50.0, ts="t1", client_order_id="snp-1"))
    b._fills.append(Fill(fill_id="f-me", symbol="US.MARA", side="BUY", qty=1,
                         price=20.0, ts="t2", client_order_id="at-abc"))
    ground_truth_sync(b, db, owned_only=True)
    rows = db._conn.execute("SELECT symbol FROM fills").fetchall()
    assert {r[0] for r in rows} == {"US.MARA"}           # SNP fill never ingested
```

Also add (same file) a `_flatten_all` scoping test: seed a `SimBroker` with an owned position (record an `at-` trade for it in the db) plus a foreign position, build a `TradeEngine` with `_cfg("SHARED")` and the db, call `engine._flatten_all(broker.get_account(), "halt-test")`, and assert only the owned symbol got a SELL fill.

- [ ] **Step 2: Run to verify failure** — FAIL (`ImportError: cancel_tracked_orders`; StopManager cancels the foreign order today).

- [ ] **Step 3: Implement**

`lifecycle.py`:

```python
def cancel_tracked_orders(broker: Broker, db: DB) -> int:
    """SHARED-mode cancel (V11): cancel only orders AutoTrader placed (present
    in the trades projection). Foreign orders — e.g. the SNP bot's — are left
    untouched. None book => logged no-op (same posture as cancel_all)."""
    open_orders = broker.get_open_orders()
    if open_orders is None:
        logger.error("cancel_tracked: open-orders query failed — nothing cancelled")
        return 0
    cancelled = 0
    for ack in open_orders:
        boid = ack.broker_order_id
        if not boid or db.get_trade_by_broker_order_id(boid) is None:
            continue          # foreign or unidentifiable: not ours to cancel
        try:
            broker.cancel_order(boid)
            cancelled += 1
        except Exception as e:
            logger.error("cancel_tracked: cancel %s failed: %s", boid, e)
    logger.info("cancel_tracked: %d tracked order(s) cancelled", cancelled)
    return cancelled
```

`ground_truth_sync(broker, db, since=None, owned_only=False)`:

```python
    fills = broker.reconcile_fills(since)
    if fills is None:
        ...
    else:
        if owned_only:
            fills = [f for f in fills
                     if f.client_order_id and f.client_order_id.startswith("at-")]
        new = db.record_fills(fills)
```

and for positions when `owned_only`:

```python
    if snap.positions_loaded:
        positions = list(snap.positions)
        if owned_only:
            owned = db.owned_symbols() | {f.symbol for f in (fills or [])
                                          if f.client_order_id
                                          and f.client_order_id.startswith("at-")}
            positions = [p for p in positions if p.symbol in owned]
        db.replace_positions(positions)
```

`stops.py` `reconcile` — orphan pass:

```python
        shared = getattr(self._cfg, "account_ownership", "SOLE") == "SHARED"
        for ack in open_orders:
            boid = ack.broker_order_id
            if not boid:
                continue
            row = self._db.get_trade_by_broker_order_id(boid)
            if shared and row is None:
                logger.info("stop reconcile: foreign order %s left untouched "
                            "(SHARED ownership)", boid)
                continue
            ...
```

attach pass, after building `held`:

```python
        if shared:
            owned = self._db.owned_symbols()
            held = {s: q for s, q in held.items() if s in owned}
```

`main.py`:

```python
    def cancel_working_orders(self) -> None:
        """SOLE: whole-account cancel (today's behavior). SHARED: cancel only
        our tracked orders — the SNP bot's book is not ours to sweep (C6)."""
        if self._cfg.account_ownership == "SHARED" and self._db is not None:
            from autotrader.lifecycle import cancel_tracked_orders
            cancel_tracked_orders(self._b, self._db)
        else:
            self._b.cancel_all()
```

`shutdown()` and the `apply_risk_check` HALT branch replace `self._b.cancel_all()` with `self.cancel_working_orders()` (keep the surrounding try/except and log text). `_flatten_all` gains the scope filter:

```python
        owned = None
        if self._cfg.account_ownership == "SHARED" and self._db is not None:
            owned = self._db.owned_symbols()
        for p in snapshot.positions:
            if p.qty <= 0:
                continue
            if owned is not None and p.symbol not in owned:
                logger.info("flatten: foreign position %s left alone (SHARED)", p.symbol)
                continue
```

`runner.py`: `__init__` gains `owned_only: bool = False`; every `ground_truth_sync(self._broker, self._db)` call becomes `ground_truth_sync(self._broker, self._db, owned_only=self._owned_only)`; the `EOD_CANCEL_ORDERS` branch calls `self._engine.cancel_working_orders()` instead of `self._broker.cancel_all()`. `main()` passes `owned_only=(cfg.account_ownership == "SHARED")` and keeps the watchdog's reconcile lambda consistent: `reconcile=lambda: ground_truth_sync(broker, db, owned_only=(cfg.account_ownership == "SHARED"))`.

- [ ] **Step 4: Run tests** — `uv run pytest tests/test_shared_ownership_scoping.py tests/ -q` → PASS; whole suite green (SOLE default keeps all existing tests unaffected).

- [ ] **Step 5: Commit**

```bash
git add autotrader/stops.py autotrader/lifecycle.py autotrader/main.py autotrader/runner.py tests/test_shared_ownership_scoping.py
git commit -m "feat(engine): SHARED ownership scopes sweeps/flatten/fills to own book (C6)"
```

---

### Task 18: V8b — Real-time alerts (HALT, watchdog, loop errors) + EOD fills-sync + halts trail

**Files:**
- Modify: `autotrader/main.py` (`apply_risk_check` alert; `build_engine`/`main()` wiring), `autotrader/runner.py`
- Test: `tests/test_operational_alerts.py` (new)

**Interfaces:**
- Consumes: `AlertSink` (Task 2; runner already has `alerts` from Task 3), `db.record_halt`.
- Produces:
  - `TradeEngine.__init__` gains `alerts: Optional[AlertSink] = None`; `apply_risk_check` HALT sends `sink.send(f"🛑 HARD HALT — {reason}: book flattened, orders cancelled, trading halted for the day", key=f"halt:{date}")`.
  - `SessionRunner.run_once` returning `HALTED_UNHEALTHY` alerts **once per episode** (`key="watchdog-unhealthy"`) and records `db.record_halt("watchdog unhealthy: connection not restored")` once per episode; on the next healthy iteration the key is `reset` (a later outage alerts again).
  - `SessionRunner.run` counts consecutive `run_once` exceptions; at 5 it alerts (`key="loop-errors"`); a clean iteration resets the counter and the key. The except block logs with `exc_info=True`.
  - `EOD_CANCEL_ORDERS` job syncs fills before recording perf: `ground_truth_sync(...)` before `self._record_perf()` (I6 — 15:30–16:30 fills appear in the day's final row and report).
  - `main()` constructs one `AlertSink(slack_url)` and passes it to both the engine (`alerts=`) and the runner (`alerts=`).

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_operational_alerts.py
from datetime import datetime

from autotrader.alerts import AlertSink
from autotrader.lifecycle import EntryGate
from autotrader.runner import SessionRunner
from autotrader.scheduler import LifecycleScheduler


class _SickWatch:
    def ensure_healthy(self):
        return False


class _DB:
    def __init__(self):
        self.halts = []
    def record_halt(self, reason):
        self.halts.append(reason)
        return 1
    def get_state(self, k): return None
    def set_state(self, k, v): pass


def _runner(posted, watch, db=None):
    class _Eng:
        def tick(self):
            class R: action = "NO_SIGNAL"
            return R()
    return SessionRunner(engine=_Eng(), broker=None, db=db,
                         gate=EntryGate(enabled=True),
                         scheduler=LifecycleScheduler(), watchdog=watch,
                         clock=None, sleep=lambda s: None,
                         alerts=AlertSink("http://x",
                                          post=lambda u, p: posted.append(p["text"]) or 200))


def test_unhealthy_alerts_once_per_episode_and_records_halt():
    posted, db = [], _DB()
    r = _runner(posted, _SickWatch(), db)
    now = datetime(2026, 7, 6, 10, 0)
    assert r.run_once(now) == "HALTED_UNHEALTHY"
    assert r.run_once(now) == "HALTED_UNHEALTHY"
    watchdog_alerts = [t for t in posted if "watchdog" in t.lower() or "unhealthy" in t.lower()]
    assert len(watchdog_alerts) == 1                     # once per episode
    assert len(db.halts) == 1


def test_halt_sends_slack(tmp_path):
    from autotrader.config import RiskConfig
    from autotrader.main import TradeEngine
    from autotrader.sim_broker import SimBroker
    from autotrader.strategies.breakout import BreakoutParams, BreakoutStrategy

    class _LossBroker(SimBroker):
        def get_account(self):
            snap = super().get_account()
            object.__setattr__(snap, "day_pnl", -5000.0)
            return snap

    posted = []
    cfg = RiskConfig(trading_env="PAPER", min_confidence=0.5, max_order_notional=1e6,
                     max_position_qty=1000, daily_loss_limit=500, max_gross_exposure=1e6,
                     allowed_symbols=frozenset(), daily_loss_halt=1000.0)
    strat = BreakoutStrategy(BreakoutParams(symbol="US.T", stop_loss_pct=0.05,
                                            take_profit_pct=0.10, confidence=0.9))
    eng = TradeEngine(_LossBroker({"US.T": 1.0}), strat, cfg, order_qty=1,
                      audit_path=str(tmp_path / "a.jsonl"),
                      alerts=AlertSink("http://x",
                                       post=lambda u, p: posted.append(p["text"]) or 200))
    eng.apply_risk_check(datetime(2026, 7, 6, 13, 30))
    assert any("HALT" in t for t in posted)
```

Also add a runner test asserting the consecutive-error alert: an engine whose `tick` raises makes `run` (driven with a `stop` that allows ~6 iterations) post exactly one alert containing "consecutive".

- [ ] **Step 2: Run to verify failure** — FAIL (`unexpected keyword 'alerts'` on TradeEngine; no watchdog alert today).

- [ ] **Step 3: Implement**

`TradeEngine.__init__`: add `alerts=None` → `self._alerts`. `apply_risk_check` HALT branch (after `record_halt`):

```python
            if self._alerts is not None:
                self._alerts.send(
                    f"🛑 HARD HALT — {reason}. Book flattened (owned positions), "
                    f"orders cancelled, trading halted for the day.",
                    key=f"halt:{now.date().isoformat()}")
```

`runner.py` `run_once`, replace the unhealthy return:

```python
        if not self._watch.ensure_healthy():
            if not self._unhealthy_episode:
                self._unhealthy_episode = True
                logger.error("watchdog exhausted — connection not restored")
                if self._alerts is not None:
                    self._alerts.send(
                        "⚠ TRADER UNHEALTHY — watchdog could not restore the "
                        "OpenD connection; loop is idling. Check OpenD.",
                        key="watchdog-unhealthy")
                if self._db is not None:
                    self._db.record_halt("watchdog unhealthy: connection not restored")
            return "HALTED_UNHEALTHY"
        if self._unhealthy_episode:
            self._unhealthy_episode = False
            if self._alerts is not None:
                self._alerts.reset("watchdog-unhealthy")
```

(`self._unhealthy_episode = False` initialized in `__init__`.)

`run()`:

```python
        errors = 0
        while not stop():
            try:
                action = self.run_once(self._clock.now_est())
                logger.debug("run_once -> %s", action)
                errors = 0
                if self._alerts is not None:
                    self._alerts.reset("loop-errors")
            except Exception as e:  # never let one bad iteration kill the session
                errors += 1
                logger.error("run_once error: %s", e, exc_info=True)
                if errors >= 5 and self._alerts is not None:
                    self._alerts.send(
                        f"⚠ TRADER DEGRADED — {errors} consecutive loop errors; "
                        f"latest: {e}", key="loop-errors")
            self._sleep(self._loop_interval)
```

`EOD_CANCEL_ORDERS` branch — sync before perf:

```python
        elif job == EOD_CANCEL_ORDERS:
            self._gate.close()
            if self._broker is not None:
                self._engine.cancel_working_orders()
                if self._db is not None:   # I6: late fills (15:30–16:30) must land
                    ground_truth_sync(self._broker, self._db,
                                      owned_only=self._owned_only)
                self._record_perf()
```

`main()` wiring:

```python
    from autotrader.alerts import AlertSink
    alerts = AlertSink(slack_url)
```

pass `alerts=alerts` to `build_engine` (add the passthrough kwarg there) and to `SessionRunner(...)`.

- [ ] **Step 4: Run tests** — `uv run pytest tests/test_operational_alerts.py tests/ -q` → PASS.

- [ ] **Step 5: Commit**

```bash
git add autotrader/main.py autotrader/runner.py tests/test_operational_alerts.py
git commit -m "feat(ops): HALT/watchdog/loop-error Slack alerts; EOD fills-sync (V8b, I6)"
```

---

### Task 19: V8c — Supervision: launchd units, file logging, heartbeat, backups

**Files:**
- Create: `deploy/com.autotrader.trader.plist`, `deploy/com.autotrader.webhook.plist`, `deploy/com.autotrader.backup.plist`, `deploy/backup_autotrader.sh`, `deploy/README.md`
- Modify: `autotrader/runner.py` (heartbeat), `autotrader/main.py` (file logging)
- Test: `tests/test_heartbeat.py` (new)

**Interfaces:**
- Consumes: `SessionRunner.run` loop, `logging` setup in `main()`.
- Produces:
  - `SessionRunner.__init__` gains `heartbeat: Optional[Callable[[], None]] = None, heartbeat_every: int = 60`; `run()` invokes `heartbeat()` every `heartbeat_every` iterations (first at iteration 1 so a fresh start pings immediately), wrapped in try/except (never breaks the loop). `main()` wires it when `AUTOTRADER_HEARTBEAT_URL` is set, using a 5-second-timeout `urllib.request.urlopen(url)` GET.
  - `main()` logging: when `AUTOTRADER_LOG_DIR` is set, add a `logging.handlers.RotatingFileHandler(<dir>/trader.log, maxBytes=10_000_000, backupCount=5)` alongside stdout.
  - launchd units (labels `com.autotrader.*` — the SNP bot owns `com.bot.trading`): `KeepAlive` + `RunAtLoad` true, `WorkingDirectory` the repo, `ProgramArguments` `[/opt/homebrew/bin/uv, run, python, -m, autotrader.main]` (webhook: `-m autotrader.signals.webhook`), `EnvironmentVariables` sourcing note (launchd cannot `source` — the plist embeds a `/bin/zsh -lc 'source config/risk.config && source config/secure.config && exec uv run python -m autotrader.main'` wrapper instead; write it that way), `StandardOutPath`/`StandardErrorPath` under `~/Library/Logs/autotrader/`. Backup plist: `StartCalendarInterval` 17:30 running `deploy/backup_autotrader.sh`.
  - `backup_autotrader.sh`: `sqlite3 ~/.autotrader.db "VACUUM INTO '$BACKUP_DIR/autotrader-$(date +%F).db'"`, copy `~/.autotrader_trade_audit.jsonl` (and legacy `~/.futu_trade_audit.jsonl` if present) to dated copies, prune backups older than 30 days, `set -euo pipefail`.
  - `deploy/README.md`: install (`launchctl load -w ~/Library/LaunchAgents/...`), verify, uninstall, power settings (`sudo pmset -a sleep 0 displaysleep 10` guidance), and the **manual verification checklist**: kill the trader process → launchd restarts it; reboot → both agents come back; heartbeat URL shows pings; backup file appears after 17:30.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_heartbeat.py
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
```

- [ ] **Step 2: Run to verify failure** — FAIL (`unexpected keyword 'heartbeat'`).

- [ ] **Step 3: Implement** — runner: store both params; in `run()` add an iteration counter; before `self._sleep(...)`:

```python
            it += 1
            if self._heartbeat is not None and (it - 1) % self._heartbeat_every == 0:
                try:
                    self._heartbeat()
                except Exception as e:    # dead-man ping must never kill the loop
                    logger.warning("heartbeat failed: %s", e)
```

`main()` wiring + logging:

```python
    hb_url = os.getenv("AUTOTRADER_HEARTBEAT_URL")
    heartbeat = None
    if hb_url:
        import urllib.request
        def heartbeat():
            urllib.request.urlopen(hb_url, timeout=5)
    log_dir = os.getenv("AUTOTRADER_LOG_DIR")
    if log_dir:
        import logging.handlers
        os.makedirs(os.path.expanduser(log_dir), exist_ok=True)
        fh = logging.handlers.RotatingFileHandler(
            os.path.join(os.path.expanduser(log_dir), "trader.log"),
            maxBytes=10_000_000, backupCount=5)
        fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        logging.getLogger().addHandler(fh)
```

(pass `heartbeat=heartbeat, heartbeat_every=int(os.getenv("AUTOTRADER_HEARTBEAT_EVERY", "60"))` to `SessionRunner`). Then write the four deploy files per the Interfaces block — full plists, not sketches.

- [ ] **Step 4: Run tests + shell-check** — `uv run pytest tests/test_heartbeat.py tests/ -q` → PASS; `plutil -lint deploy/*.plist` → OK; `zsh -n deploy/backup_autotrader.sh` → OK.

- [ ] **Step 5: Commit**

```bash
git add deploy/ autotrader/runner.py autotrader/main.py tests/test_heartbeat.py
git commit -m "feat(ops): launchd supervision, rotating logs, dead-man heartbeat, nightly backups (V8c)"
```

---

### Task 20: V9 — Config completeness: strategy params, audit path, holidays horizon, knob docs

**Files:**
- Modify: `autotrader/config.py`, `autotrader/main.py` (`main()`), `config/risk.config.example`
- Test: `tests/test_config_v9.py` (new)

**Interfaces:**
- Consumes: `BreakoutParams`, `_DEFAULT_2026_NYSE_HOLIDAYS`, `RiskConfig.market_holidays`.
- Produces:
  - `main()` builds `BreakoutParams` from env: `STRATEGY_STOP_LOSS_PCT` (default `0.05`), `STRATEGY_TAKE_PROFIT_PCT` (`0.10`), `STRATEGY_CONFIDENCE` (`0.7`) — no hardcoded risk values (CLAUDE.md).
  - Audit path from env: `AUTOTRADER_AUDIT_PATH`, default `~/.autotrader_trade_audit.jsonl` (**new default** — distinct from the vendored skills'/SNP's `~/.futu_trade_audit.jsonl`, per V11; the old file is left in place for the skills).
  - Holiday default extended: rename the constant to `_DEFAULT_NYSE_HOLIDAYS` covering 2026 **and** 2027 (2027 dates: `2027-01-01,2027-01-18,2027-02-15,2027-03-26,2027-05-31,2027-06-18,2027-07-05,2027-09-06,2027-11-25,2027-12-24` — implementer MUST verify against the official NYSE calendar and note the source in a comment).
  - New pure helper `config.holiday_horizon_warning(holidays: FrozenSet[date], today: date, days: int = 60) -> Optional[str]` — returns a warning string when `max(holidays)` is within `days` of `today` (or holidays empty), else None. `main()` logs it when non-None.
  - `config/risk.config.example` gains commented entries for every knob added this round and the previously undocumented W-series ones: `RISK_MARKET_HOLIDAYS`, `RISK_LIMIT_ORDERS_ENABLED`, `RISK_ORDER_CAP_BPS`, `RISK_ORDER_CAP_TICKS`, `RISK_ESCALATION_DWELL_SECONDS`, `RISK_UNREALIZED_LOSS_GATE`, `RISK_ACCOUNT_OWNERSHIP`, `RISK_SIGNALS_ENABLED` (added in this task's config change, consumed below), `STRATEGY_STOP_LOSS_PCT/TAKE_PROFIT_PCT/CONFIDENCE`, `AUTOTRADER_AUDIT_PATH`, `AUTOTRADER_SIGNAL_TTL_HOURS`, `AUTOTRADER_WEBHOOK_TOKEN`, `AUTOTRADER_WEBHOOK_FRESHNESS_MIN`, `AUTOTRADER_HEARTBEAT_URL`, `AUTOTRADER_LOG_DIR`, `AUTOTRADER_SNAPSHOT_CACHE_TICKS` — each with a one-line comment and its default.
  - **`RISK_SIGNALS_ENABLED` (stage-model flag):** `RiskConfig.signals_enabled: bool = True` (env `RISK_SIGNALS_ENABLED`); `main()` skips inbox construction when False and logs "external signals DISABLED (stage gating)". This is the Stage-1 switch from the spec §3.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_config_v9.py
from datetime import date

from autotrader.config import holiday_horizon_warning, load_risk_config


def test_signals_enabled_default_true(monkeypatch):
    monkeypatch.delenv("RISK_SIGNALS_ENABLED", raising=False)
    assert load_risk_config().signals_enabled is True


def test_signals_can_be_disabled(monkeypatch):
    monkeypatch.setenv("RISK_SIGNALS_ENABLED", "0")
    assert load_risk_config().signals_enabled is False


def test_default_holidays_extend_past_2026(monkeypatch):
    monkeypatch.delenv("RISK_MARKET_HOLIDAYS", raising=False)
    assert max(load_risk_config().market_holidays).year >= 2027


def test_horizon_warning_when_calendar_nearly_expired():
    hols = frozenset({date(2026, 12, 25)})
    assert holiday_horizon_warning(hols, today=date(2026, 11, 20)) is not None
    assert holiday_horizon_warning(hols, today=date(2026, 6, 1)) is None
```

- [ ] **Step 2: Run to verify failure** — FAIL (missing attribute/helper; max holiday year is 2026).

- [ ] **Step 3: Implement** — config field + loader entry (`signals_enabled=_b("RISK_SIGNALS_ENABLED", True)`), extended holiday constant, and:

```python
def holiday_horizon_warning(holidays, today, days: int = 60):
    """Warn when the configured holiday calendar is about to run out — the
    silent failure mode is 'trades on holidays' (V9)."""
    if not holidays:
        return "RISK_MARKET_HOLIDAYS is EMPTY — holiday gating is off"
    horizon = (max(holidays) - today).days
    if horizon < days:
        return (f"RISK_MARKET_HOLIDAYS ends {max(holidays).isoformat()} "
                f"({horizon}d away) — extend the calendar before it expires")
    return None
```

`main()` changes: BreakoutParams from env; `audit = os.path.expanduser(os.getenv("AUTOTRADER_AUDIT_PATH", "~/.autotrader_trade_audit.jsonl"))`; log `holiday_horizon_warning(cfg.market_holidays, date.today())` when non-None; wrap inbox construction in `if inbox_dir and cfg.signals_enabled:` (log the disabled case). Then update `config/risk.config.example` with the full knob list.

- [ ] **Step 4: Run tests** — `uv run pytest tests/test_config_v9.py tests/ -q` → PASS.

- [ ] **Step 5: Commit**

```bash
git add autotrader/config.py autotrader/main.py config/risk.config.example tests/test_config_v9.py
git commit -m "feat(config): strategy params, audit path, RISK_SIGNALS_ENABLED, holiday horizon (V9)"
```

---

### Task 21: V10a — CI workflow + working-tree hygiene

**Files:**
- Create: `.github/workflows/tests.yml`
- Modify: `.gitignore`
- Commit-as-is: `docs/research/` (5 files), `docs/routinesignal-ticker.json`, `uv.lock`

**Interfaces:** none consumed by later tasks; CI enforces the suite from here on.

- [ ] **Step 1: Write the workflow**

```yaml
# .github/workflows/tests.yml
name: tests
on:
  push:
    branches: [develop, main]
  pull_request:
    branches: [develop, main]
jobs:
  offline-suite:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: astral-sh/setup-uv@v5
      - run: uv sync
      - run: uv run pytest -q --ignore=tests/test_moomoo_broker_live.py
```

Implementer: check `pyproject.toml` — if there is no `[project]`/dependency table that `uv sync` can use, replace the two uv steps with `uv venv && uv pip install -r dashboard/requirements.txt pydantic flask pytest && uv run pytest -q ...` matching whatever the RUNBOOK's verified install command is. The job must pass on a fresh clone; verify locally with `uv run pytest -q --ignore=tests/test_moomoo_broker_live.py` before committing.

- [ ] **Step 2: Hygiene**

```bash
# referenced by committed docs -> track them
git add docs/research/ docs/routinesignal-ticker.json uv.lock
# session scratch -> ignore
printf "plan/\nstate.md\ndocs/routinesignal-options.json\n" >> .gitignore
git add .gitignore
```

(`docs/superpowers/plans/*.md` untracked plan files from June are historical working docs — add them too: `git add docs/superpowers/plans/`.)

- [ ] **Step 3: Verify** — `git status --short` shows no untracked files except intentional ignores; `uv run pytest -q --ignore=tests/test_moomoo_broker_live.py` green.

- [ ] **Step 4: Commit**

```bash
git add .github/workflows/tests.yml
git commit -m "chore(ci): offline pytest on push/PR; track referenced docs; ignore session scratch (V10a)"
```

---

### Task 22: V10b — CUTOVER.md, PRE-LIVE.md v2, RUNBOOK truth-pass

**Files:**
- Create: `CUTOVER.md`
- Rewrite: `PRE-LIVE.md`
- Modify: `RUNBOOK.md`

**Interfaces:** consumes everything above; produces the operator-facing gate. No code.

- [ ] **Step 1: Write `CUTOVER.md`** with exactly these sections (write real content, not headings):
  1. **Preconditions** — every PRE-LIVE v2 Stage-1 box checked; supervised paper session in `SOLE` mode with the SNP bot stopped (the SHARED/SOLE rehearsal gap); backups verified restorable; alerts verified firing (force a test HALT on paper).
  2. **The two-file guard revision** — the ONLY sanctioned edit path: `autotrader/main.py` (the `trading_env != "PAPER"` refusal) and `autotrader/risk_core.py` (the non-PAPER rejection); each edit gets human review; `RISK_ACCOUNT_OWNERSHIP=SOLE` is asserted by config (LIVE+SHARED refuses to start).
  3. **Stage 1 flip** — exact env diff (`RISK_TRADING_ENV=LIVE`, `RISK_SIGNALS_ENABLED=0`, `RISK_LIMIT_ORDERS_ENABLED=0`, `RISK_REBALANCE_ENABLED=0`, overlays empty), first-session reduced caps (halve `RISK_MAX_ORDER_NOTIONAL`, `RISK_MAX_POSITION_QTY`, `RISK_DAILY_LOSS_LIMIT/HALT` — list concrete example values next to current defaults), manual GUI trade unlock (never SDK).
  4. **Kill-switch test** — during the first live session with a tiny position: Ctrl-C the trader → verify cancel-on-shutdown removed tracked working orders in the OpenD GUI; document `launchctl unload` as the supervised-mode stop.
  5. **Rollback** — flip `RISK_TRADING_ENV=PAPER` back, restart, verify PAPER in the startup log; if orders are stuck live: manual GUI cancel; restore DB from last backup if the projection is suspect.
  6. **Stage promotion criteria** — "clean session" defined as: no Critical alert, no HALTED_UNHEALTHY episode, EOD report delivered, stops attached for every entry (check the reconcile log line), zero foreign-order incidents. Stage 1→2 after **5** clean sessions; Stage 2→3 after **5** more plus a logged rebalance dry-run (run `REBALANCE` with `RISK_REBALANCE_ENABLED=0` still off but plan-logging on — describe using the existing `compute_plan` log output).
  7. **SNP-bot coexistence check** — post-flip, confirm both processes healthy; AutoTrader on the LIVE account, SNP still SIMULATE; watch OpenD quote-quota headroom for a session.
- [ ] **Step 2: Rewrite `PRE-LIVE.md`** as the staged gate: Stage-1/2/3 checkbox sections mapping to V1–V11 (each with the task's verifiable artifact: test name or deploy file), the operational boxes (supervision reboot test, backup restore drill, alert live-fire, holidays horizon, RUNBOOK refreshed), and a "superseded" note pointing the old W1–W8 list at the spec. Keep the deferred-minors list (carry it over, updated per spec §6).
- [ ] **Step 3: RUNBOOK truth-pass** — fix the four drift classes found in review: (a) test count: replace "expect: 118 passed" with "expect: the count CI enforces — run `uv run pytest -q`"; (b) §5 lifecycle table: all **8** jobs with current names/times (PRE_OPEN_SYNC 08:30, ENTRY_OPEN 09:45, REBALANCE 12:30, RISK_CHECK_MID 13:30, RISK_CHECK_LATE 15:00, RISK_SWEEP 15:30, EOD_CANCEL_ORDERS 16:15, EOD_REPORT 16:30) and the mark-on-success retry semantics; (c) §7: replace the "entry left unprotected — watch the dashboard" caveat with the StopManager entry-fill-confirm + intraday reconcile behavior; (d) config reference: add every knob from Task 20's example file, the SHARED-mode quota note (10 refresh/30s shared with the SNP bot — keep `AUTOTRADER_SNAPSHOT_CACHE_TICKS` ≥ 6), the launchd operations section (pointing at `deploy/README.md`), and replace the real ngrok hostname in §12b with `<your-tunnel>.ngrok-free.dev`.
- [ ] **Step 4: Verify** — `grep -n "118 passed" RUNBOOK.md` → no hits; `grep -n "EOD_FLATTEN" RUNBOOK.md` → no hits (except a "renamed" note if kept); `grep -rn "unthawed" RUNBOOK.md` → no hits; every `RISK_*`/`AUTOTRADER_*` env var read in `autotrader/` appears in RUNBOOK: cross-check with `grep -rhoE "(RISK|AUTOTRADER|STRATEGY)_[A-Z_]+" autotrader/ | sort -u`.
- [ ] **Step 5: Commit**

```bash
git add CUTOVER.md PRE-LIVE.md RUNBOOK.md
git commit -m "docs(ops): CUTOVER procedure, staged PRE-LIVE v2 gate, RUNBOOK truth-pass (V10b)"
```

---

## Execution order & parallelism

Sequential spine: **1 → 2 → 3 → 4 → 5** (rig, alerts, scheduler, stops). Then **6–11** (core risk, each independent of the others but all touch `main.py`/`risk_check.py` — run sequentially to avoid merge friction). **12–15** (ingress/options) are independent of 6–11 in content but keep sequential execution for the same single-branch reason. **16 → 17** (V11) after 11. **18** after 2, 9, 17 (it touches the HALT branch and EOD job). **19 → 20 → 21 → 22** close out. A task's reviewer gate must pass before the next task starts.

## Plan self-review notes (already applied)

- Spec coverage: V1→T1, V2→T3, V3→T4+T5, V4→T6–T9, V5→T10+T11, V6→T12–T14, V7→T15, V8→T2+T18+T19, V9→T20, V10→T21+T22, V11→T16+T17. `RISK_SIGNALS_ENABLED` (spec §3) lands in T20. Spec §5 regression list: C1=T3, C2=T4/T5, C3=T13, C4=T15, C5=T18, C6=T17.
- Types: `AlertSink.send/reset` (T2) match usage in T3/T18; `mark_fired(name, now)` (T3) matches runner use; `_confirm_off_book(ack)` (T4) reused nowhere else by name; `cancel_tracked_orders(broker, db)` and `cancel_working_orders()` (T17) match T18's EOD usage; `owned_only` kwarg consistent across T17/T18.
- Known judgment calls for the reviewer: T3 changes `poll()` semantics (existing tests updated, at-most-once preserved via `mark_fired`); T20 changes the default audit path (old `~/.futu_trade_audit.jsonl` remains for the vendored skills); T14's arrival-order guarantee applies to files written after the rename (one-time transition).
