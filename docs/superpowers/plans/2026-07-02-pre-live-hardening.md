# Pre-Live Hardening Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close every live-path gap from the 2026-07-02 full-codebase review (spec: `docs/superpowers/specs/2026-07-02-pre-live-hardening-design.md`) — stop re-attach, fail-loud broker queries, trading calendar, durable engine state, escalation dwell, CLOSE-leg exemption, single performance writer, PRE-LIVE gate.

**Architecture:** All changes compose with the existing spine: pure risk core, `OrderRouter` audit-before-place, `SimBroker` for offline tests, injected clocks/sleeps. New modules: `autotrader/stops.py` (StopManager), `autotrader/market_calendar.py`. New DB table: `engine_state`. Everything else is targeted edits to `main.py`, `runner.py`, `scheduler.py`, `moomoo_broker.py`, `lifecycle.py`, `risk_core.py`, `config.py`, `db.py`, `sim_broker.py`, `broker.py`, `domain.py`.

**Tech Stack:** Python 3 stdlib only (sqlite3, dataclasses, zoneinfo, json). pytest. No new dependencies.

## Global Constraints

- Baseline: 519 tests collect on branch `feat/autotrader-paper-v1`. Full suite (`python -m pytest -q`) must be green before every commit. No OpenD/SDK needed for any new test.
- **Do NOT touch** the uncommitted WIP files: `autotrader/signals/coerce.py`, `autotrader/signals/normalize.py`, `tests/test_coerce.py`, `tests/test_signal_normalize.py`. Never `git add -A` / `git add .` — stage files explicitly.
- Never modify anything under `skills/` (vendored Futu bundles).
- All new knobs come from env/config, never hardcoded at call sites: `RISK_MARKET_HOLIDAYS`, `RISK_ESCALATION_DWELL_SECONDS`, `AUTOTRADER_SNAPSHOT_CACHE_TICKS`.
- Unknown broker state is never treated as success or absence (None ≠ empty list — the `_positions()` precedent at `moomoo_broker.py:287`).
- Injected sleeps only (`hedge_confirm_sleep` pattern); no `time.sleep` in loops or as readiness checks.
- New defaults must be behavior-preserving for existing tests: `snapshot_cache_ticks=1`, `escalation_sleep`/`trading_day_fn`/`stop_manager`/`state_get`/`state_set` default `None` (= today's behavior). Production behavior is enabled by explicit wiring in `main()`.
- Do not spawn nested agents from any implementation task; do the work directly.
- Commit messages: conventional-commit style matching the repo (`feat(engine): …`, `fix(broker): …`), ending with `Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>`.

---

### Task 1: MoomooBroker queries fail loudly (None ≠ empty)

**Files:**
- Modify: `autotrader/moomoo_broker.py:243-259` (`get_open_orders`), `:233-241` (`cancel_all`), `:311-312` (`get_open_orders_count`), `:320-347` (`reconcile_fills`), `:349-382` (`_fills_from_orders`)
- Modify: `autotrader/broker.py:34-35` (contract docstrings)
- Modify: `autotrader/sim_broker.py` (failure-injection flags for later tasks)
- Test: `tests/test_moomoo_broker_offline.py`

**Interfaces:**
- Consumes: existing `_broker_with_trade(trade, env)` helper and `_FakeCommon`/`_DF` fakes in `tests/test_moomoo_broker_offline.py`.
- Produces: `Broker.get_open_orders() -> Optional[List[OrderAck]]` and `Broker.reconcile_fills(since) -> Optional[List[Fill]]` where **`None` = query failed** (rate-limit timeout or non-OK ret) and `[]` = genuinely empty. `SimBroker.fail_open_orders: bool` and `SimBroker.fail_reconcile_fills: bool` attributes (default `False`) that force `None` returns. Tasks 2–5 and 11 rely on these.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_moomoo_broker_offline.py` (reuse the module's existing `_broker_with_trade` helper):

```python
class _FailingOrderListTrade:
    """order_list_query fails (e.g. OpenD timeout) — the book is UNKNOWN."""
    def __init__(self):
        self.cancelled = []

    def order_list_query(self, **kwargs):
        return -1, "query timeout"

    def modify_order(self, **kwargs):
        self.cancelled.append(kwargs.get("order_id"))
        return 0, None


class _EmptyOrderListTrade:
    def order_list_query(self, **kwargs):
        return 0, None  # RET_OK, empty frame -> genuinely no working orders


def test_get_open_orders_returns_none_on_query_failure():
    b = _broker_with_trade(_FailingOrderListTrade())
    assert b.get_open_orders() is None          # unknown, NOT "no orders"


def test_get_open_orders_empty_book_is_empty_list():
    b = _broker_with_trade(_EmptyOrderListTrade())
    assert b.get_open_orders() == []


def test_cancel_all_is_noop_when_book_unknown():
    trade = _FailingOrderListTrade()
    b = _broker_with_trade(trade)
    b.cancel_all()                               # must not raise
    assert trade.cancelled == []                 # and must not guess-cancel


def test_reconcile_fills_returns_none_when_paper_order_query_fails():
    b = _broker_with_trade(_FailingOrderListTrade())  # SIMULATE -> _fills_from_orders
    assert b.reconcile_fills(since=None) is None


class _FailingDealTrade:
    def deal_list_query(self, **kwargs):
        return -1, "query timeout"


def test_reconcile_fills_returns_none_when_live_deal_query_fails():
    b = _broker_with_trade(_FailingDealTrade(), env="REAL")
    assert b.reconcile_fills(since=None) is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_moomoo_broker_offline.py -q`
Expected: the 5 new tests FAIL (current code returns `[]` on failure; `cancel_all` logs `len(None)`-style behavior does not occur because `[]` is returned — the None assertions fail).

- [ ] **Step 3: Implement**

In `autotrader/moomoo_broker.py`, replace `get_open_orders` and `cancel_all`:

```python
    def cancel_all(self) -> None:
        orders = self.get_open_orders()
        if orders is None:
            # Book unknown — cancelling by a stale/guessed list is worse than
            # doing nothing; the next lifecycle reconcile retries.
            logger.error("cancel_all: open-orders query failed — book unknown, nothing cancelled")
            return
        logger.info("cancel_all: %d open order(s) to cancel", len(orders))
        for ack in orders:
            if ack.broker_order_id:
                try:
                    self.cancel_order(ack.broker_order_id)
                except BrokerError:
                    pass  # best-effort flatten on shutdown; logged by cancel_order

    def get_open_orders(self) -> Optional[List[OrderAck]]:
        """Working orders, or None when the QUERY FAILED (rate-limit timeout or
        non-OK ret). None is distinct from [] (genuinely no working orders) —
        the same contract as _positions(). Callers must treat None as UNKNOWN,
        never as 'nothing is working' (a rate-limited query must not make a
        hedge look filled or a resting limit look done)."""
        if not self._refresh_rl.acquire(timeout=60.0):
            logger.warning("get_open_orders: refresh rate limit — book unknown")
            return None
        ret, data = self._trade.order_list_query(
            trd_env=self._env(), acc_id=self._acc_id, refresh_cache=True)
        if not self._ok(ret):
            logger.warning("get_open_orders: query failed: %s", data)
            return None
        if self._c.is_empty(data):
            return []
        out: List[OrderAck] = []
        for i in range(len(data)):
            row = data.iloc[i]
            status = self._c.format_enum(self._c.safe_get(row, "order_status", default="")).upper()
            state = _STATUS_MAP.get(status, OrderState.UNKNOWN)
            if state in (OrderState.PENDING, OrderState.SUBMITTED, OrderState.PARTIAL):
                out.append(OrderAck(
                    str(self._c.safe_get(row, "remark", default="")),
                    str(self._c.safe_get(row, "order_id", default="")), state, {}))
        return out
```

Update `get_open_orders_count`:

```python
    def get_open_orders_count(self) -> int:  # convenience for logs; -1 = unknown
        orders = self.get_open_orders()
        return len(orders) if orders is not None else -1
```

In `reconcile_fills`, change the two failure paths (rate limit and non-OK ret) to return `None`, keeping empty-frame as `[]`:

```python
    def reconcile_fills(self, since: Optional[str]) -> Optional[List[Fill]]:
        # (docstring: add) Returns None when the underlying query FAILED —
        # distinct from [] (no fills). Callers must skip projection updates on None.
        if self._is_paper():
            return self._fills_from_orders()
        if not self._refresh_rl.acquire(timeout=60.0):
            return None
        kwargs = dict(trd_env=self._env(), acc_id=self._acc_id, refresh_cache=True)
        if since:
            kwargs["begin_time"] = since
        ret, data = self._trade.deal_list_query(**kwargs)
        if not self._ok(ret):
            logger.warning("reconcile_fills: deal query failed: %s", data)
            return None
        if self._c.is_empty(data):
            return []
        ...  # existing row loop unchanged
```

Apply the identical split in `_fills_from_orders` (rate-limit → `None`; `not self._ok(ret)` → `None`; `is_empty` → `[]`).

In `autotrader/broker.py`, update the base-class signatures/docs:

```python
    def get_open_orders(self) -> Optional[List[OrderAck]]:
        """Working orders; None = the query FAILED (unknown book), [] = none."""
        raise NotImplementedError

    def reconcile_fills(self, since: Optional[str]) -> Optional[List[Fill]]:
        """Fills since `since`; None = the query FAILED, [] = none."""
        raise NotImplementedError
```

In `autotrader/sim_broker.py`, add failure-injection flags (test infrastructure for later tasks):

```python
        # __init__ additions (after self._chains):
        # Failure injection for tests of the None-vs-empty broker contract.
        self.fail_open_orders = False
        self.fail_reconcile_fills = False
```

```python
    def get_open_orders(self) -> Optional[List[OrderAck]]:
        if self.fail_open_orders:
            return None   # simulate a failed/rate-limited query (unknown book)
        return list(self._open.values())

    def reconcile_fills(self, since: Optional[str]) -> Optional[List[Fill]]:
        if self.fail_reconcile_fills:
            return None
        return list(self._fills)
```

- [ ] **Step 4: Run tests**

Run: `python -m pytest tests/test_moomoo_broker_offline.py -q && python -m pytest -q`
Expected: new tests PASS; full suite green (SimBroker default flags preserve all existing behavior).

- [ ] **Step 5: Commit**

```bash
git add autotrader/moomoo_broker.py autotrader/broker.py autotrader/sim_broker.py tests/test_moomoo_broker_offline.py
git commit -m "fix(broker): get_open_orders/reconcile_fills return None on query failure

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 2: Lifecycle treats unknown broker state conservatively

**Files:**
- Modify: `autotrader/lifecycle.py:28-57` (`ground_truth_sync`, `reconcile_open_orders`)
- Test: `tests/test_lifecycle.py`

**Interfaces:**
- Consumes: `SimBroker.fail_open_orders` / `fail_reconcile_fills` (Task 1); `AccountSnapshot.positions_loaded`.
- Produces: `reconcile_open_orders` returns `0` and sweeps nothing on unknown book; `ground_truth_sync` never wipes the positions table on a failed position query and never records fills from a `None` result.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_lifecycle.py` (follow the file's existing fixture style — it builds `SimBroker` + `DB(tmp_path)`; reuse its helpers if present):

```python
from autotrader.domain import AccountSnapshot, OrderRequest
from autotrader.lifecycle import ground_truth_sync, reconcile_open_orders
from autotrader.sim_broker import SimBroker
from autotrader.db import DB


def test_reconcile_open_orders_noop_when_book_unknown(tmp_path):
    """A failed open-orders query must NOT mark DB stops cancelled — the old
    []-on-failure behavior silently declared every working stop dead."""
    db = DB(str(tmp_path / "t.db"))
    db.record_trade(client_order_id="c1", symbol="US.AAPL", side="SELL", qty=5,
                    order_type="TRAILING_STOP", limit_price=None,
                    broker_order_id="sim-9", state="SUBMITTED")
    b = SimBroker(quotes={"US.AAPL": 100.0})
    b.fail_open_orders = True
    assert reconcile_open_orders(b, db) == 0
    assert db.get_open_trailing_stop("US.AAPL") == "sim-9"   # still live in the projection
    db.close()


def test_ground_truth_sync_skips_fills_when_query_fails(tmp_path):
    db = DB(str(tmp_path / "t.db"))
    b = SimBroker(quotes={"US.AAPL": 100.0}, cash=10000.0)
    b.place_order(OrderRequest(symbol="US.AAPL", side="BUY", qty=1,
                               order_type="MARKET", limit_price=None,
                               client_order_id="seed"))
    b.fail_reconcile_fills = True
    res = ground_truth_sync(b, db)
    assert res.new_fills == 0
    assert db._conn.execute("SELECT COUNT(*) FROM fills").fetchone()[0] == 0
    db.close()


def test_ground_truth_sync_keeps_positions_when_positions_not_loaded(tmp_path):
    """A stale/not-loaded snapshot (positions query failed) must not wipe the
    positions projection with its empty tuple."""
    db = DB(str(tmp_path / "t.db"))
    good = SimBroker(quotes={"US.AAPL": 100.0}, cash=10000.0)
    good.place_order(OrderRequest(symbol="US.AAPL", side="BUY", qty=3,
                                  order_type="MARKET", limit_price=None,
                                  client_order_id="seed"))
    ground_truth_sync(good, db)                    # projection now holds AAPL x3

    class _BrokenPositions(SimBroker):
        def get_account(self):
            return AccountSnapshot(cash=1.0, total_assets=1.0, day_pnl=0.0,
                                   stale=True, positions_loaded=False, positions=())

    broken = _BrokenPositions(quotes={"US.AAPL": 100.0})
    ground_truth_sync(broken, db)
    row = db._conn.execute("SELECT qty FROM positions WHERE symbol='US.AAPL'").fetchone()
    assert row is not None and row[0] == 3         # NOT wiped
    db.close()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_lifecycle.py -q`
Expected: 3 new FAILs (`reconcile_open_orders` crashes/sweeps on None; sync wipes positions and crashes on None fills).

- [ ] **Step 3: Implement**

Replace the two functions in `autotrader/lifecycle.py`:

```python
def ground_truth_sync(broker: Broker, db: DB, since: Optional[str] = None) -> SyncResult:
    snap = broker.get_account()
    if snap.positions_loaded:
        db.replace_positions(list(snap.positions))
    else:
        # Position query failed — snap.positions is an EMPTY placeholder, not
        # broker truth. Replacing would wipe the projection with nothing.
        logger.warning("ground_truth_sync: positions not loaded — projection kept as-is")
    fills = broker.reconcile_fills(since)
    if fills is None:
        logger.warning("ground_truth_sync: fills query failed — skipping fill projection")
        new = 0
    else:
        new = db.record_fills(fills)
    reconcile_open_orders(broker, db)
    logger.info("ground_truth_sync: %d position(s), %d new fill(s)",
                len(snap.positions), new)
    return SyncResult(positions=len(snap.positions), new_fills=new)


def reconcile_open_orders(broker: Broker, db: DB) -> int:
    """... (keep existing docstring, append:) A None from get_open_orders means
    the book is UNKNOWN — sweeping then would mark live stops CANCELLED off a
    failed query, so the reconcile is a logged no-op instead."""
    open_orders = broker.get_open_orders()
    if open_orders is None:
        logger.warning("reconcile_open_orders: open-orders query failed — no-op")
        return 0
    open_ids = {a.broker_order_id for a in open_orders
                if a.broker_order_id is not None}
    swept = 0
    for boid in db.open_trailing_stop_ids():
        if boid not in open_ids:
            db.mark_order_cancelled(boid)
            swept += 1
    if swept:
        logger.info("reconcile_open_orders: %d stale trailing stop(s) swept", swept)
    return swept
```

- [ ] **Step 4: Run tests**

Run: `python -m pytest tests/test_lifecycle.py -q && python -m pytest -q`
Expected: all green.

- [ ] **Step 5: Commit**

```bash
git add autotrader/lifecycle.py tests/test_lifecycle.py
git commit -m "fix(lifecycle): never sweep stops or wipe positions on a failed broker query

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 3: Engine tri-state open-orders view (hedge confirm + escalation)

**Files:**
- Modify: `autotrader/main.py:253-313` (`_submit_with_escalation`), `:440-463` (`_confirm_hedge_fill`); add `_order_working` helper
- Test: `tests/test_limit_escalation.py` (new tests appended)

**Interfaces:**
- Consumes: `Broker.get_open_orders() -> Optional[List[OrderAck]]` (Task 1), `SimBroker.fail_open_orders`.
- Produces: `TradeEngine._order_working(ack) -> Optional[bool]` — `True` still working / `False` off the book / `None` unknown. Tasks 5 and 11 reuse it.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_limit_escalation.py` (uses that file's `_cfg`/`_engine` helpers):

```python
def test_unknown_book_aborts_escalation_no_market_no_cancel(tmp_path):
    """If the open-orders query fails after the limit is submitted, the engine
    must NOT declare it filled, NOT cancel, and NOT fall through to MARKET —
    a rate-limited query must never cause a duplicate or phantom fill."""
    b = SimBroker(quotes={"US.AAPL": 100.0}, cash=1_000_000.0, spread_bps=100.0)
    b.fail_open_orders = True
    eng = _engine(b, _cfg(order_cap_bps=5.0), tmp_path)
    assert eng.tick().action == "ORDER_PLACED"
    assert b.get_account().position_qty("US.AAPL") == 0   # no MARKET fallback fired
    assert len(b._open) == 1                              # original limit still resting
```

Also append to `tests/test_limit_escalation.py` a direct unit test of the hedge fill-poll (uses this file's `_cfg`/`_engine` helpers — `_confirm_hedge_fill` is exercised directly so no overlay scaffolding is needed):

```python
def test_hedge_confirm_never_confirms_on_unknown_book(tmp_path):
    """A failed open-orders query during the hedge fill-poll must count as NOT
    confirmed (spec W2) — the old code's `[]`-on-failure made a rate-limited
    query read as 'off the book' == FILLED, silently skipping the §2.B
    fallback stop and UNHEDGED alert."""
    from autotrader.domain import OrderAck, OrderRequest, OrderState
    b = SimBroker(quotes={"US.AAPL": 100.0}, cash=1_000_000.0)
    eng = _engine(b, _cfg(), tmp_path)
    b.fail_open_orders = True
    ack = OrderAck("hedge-cid", "sim-77", OrderState.SUBMITTED, {})
    req = OrderRequest(symbol="US.AAPL260918C110000", side="SELL", qty=1,
                       order_type="MARKET", limit_price=None,
                       client_order_id="hedge-cid")
    assert eng._confirm_hedge_fill(ack, req) is False   # unknown != confirmed
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_limit_escalation.py -q`
Expected: the escalation test FAILS with `TypeError` (iterating `None`) or a MARKET fill; the hedge test FAILS with `_confirm_hedge_fill` returning `True` (unknown book read as "off the book").

- [ ] **Step 3: Implement**

Add the helper to `TradeEngine` (place after `_order_kind`):

```python
    def _order_working(self, ack) -> "Optional[bool]":
        """Tri-state open-orders membership for ack's client_order_id.
        True = still working; False = off the book (filled/terminal); None =
        the query FAILED — the caller must treat the state as UNKNOWN, never
        as 'filled' (review Important #1)."""
        open_orders = self._b.get_open_orders()
        if open_orders is None:
            return None
        return ack.client_order_id in {o.client_order_id for o in open_orders}
```

In `_submit_with_escalation`, replace the inner `_still_working` closure and its two call sites:

```python
        ack = self._router.submit(req)
        ack_req = req

        working = self._order_working(ack)
        if working is None:
            logger.warning("escalation: open-orders unknown — leaving %s as-is "
                           "(no cancel, no fallback)", ack.client_order_id)
            return ack, req
        if not working:
            return ack, req   # marketable limit filled (or terminal) on the first pass
```

and after the re-peg submit:

```python
        if evaluate(peg, snap, self._cfg, ref_price=cur).approved:
            ack = self._router.submit(peg)
            ack_req = peg
            working = self._order_working(ack)
            if working is None:
                logger.warning("escalation: open-orders unknown after re-peg — "
                               "leaving %s as-is", ack.client_order_id)
                return ack, peg
            if not working:
                return ack, peg
```

In `_confirm_hedge_fill`, replace the inner `_still_working` with tri-state (None = keep polling, never confirm):

```python
        def _resting() -> "Optional[bool]":
            return self._order_working(ack)

        first = _resting()
        if first is False:
            return True   # already off the book (terminal/filled) at first look
        for attempt in range(1, self._hedge_confirm_attempts + 1):
            self._hedge_confirm_sleep(backoff_seconds(attempt))
            state = _resting()
            if state is False:
                return True
            if state is None:
                logger.warning("hedge %s: open-orders query failed on fill-poll "
                               "attempt %d — cannot confirm", req.symbol, attempt)
        logger.warning("hedge %s still resting after %d fill-poll attempts (%s)",
                       req.symbol, self._hedge_confirm_attempts, ack.state.value)
        return False
```

- [ ] **Step 4: Run tests**

Run: `python -m pytest tests/test_limit_escalation.py -q && python -m pytest -q`
Expected: all green.

- [ ] **Step 5: Commit**

```bash
git add autotrader/main.py tests/test_limit_escalation.py
git commit -m "fix(engine): unknown open-orders book never reads as filled (hedge confirm + escalation)

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 4: Snapshot cache for refresh-token budgeting

**Files:**
- Modify: `autotrader/main.py:66-119` (`TradeEngine.__init__`, `tick`)
- Test: `tests/test_snapshot_cache.py` (create)

**Interfaces:**
- Consumes: nothing new.
- Produces: `TradeEngine(..., snapshot_cache_ticks: int = 1)` ctor param. Cached snapshots feed ONLY the strategy tick; any signal triggers a fresh `get_account()` before routing, and the cache is dropped after routing. Task 15's `build_engine` wires `AUTOTRADER_SNAPSHOT_CACHE_TICKS` (default 6).

**Why:** `tick()` costs 2 refresh tokens (accinfo + positions) every 5 s ⇒ ~24/min vs Moomoo's 20/min refill — the bucket runs dry exactly when hedge-confirm/escalation also need tokens (review Important #1 trigger). Caching NO_SIGNAL ticks cuts steady-state usage to ~4/min at the default wiring while keeping every risk decision on fresh data. (Deliberate refinement of spec §2's "runner fetches once per iteration" wording — same goal, enforced where the fetch actually lives.)

- [ ] **Step 1: Write the failing test**

Create `tests/test_snapshot_cache.py`:

```python
"""Refresh-token budgeting: NO_SIGNAL ticks reuse a cached account snapshot;
any routed signal always re-fetches fresh (cached data never reaches the risk
core). Default snapshot_cache_ticks=1 preserves today's fetch-per-tick."""
from autotrader.config import RiskConfig
from autotrader.main import TradeEngine
from autotrader.sim_broker import SimBroker
from autotrader.strategies.threshold import ThresholdStrategy, StrategyParams


class _CountingBroker(SimBroker):
    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self.account_calls = 0

    def get_account(self):
        self.account_calls += 1
        return super().get_account()


def _cfg():
    return RiskConfig(trading_env="PAPER", min_confidence=0.6,
                      max_order_notional=20000, max_position_qty=100,
                      daily_loss_limit=500, max_gross_exposure=100000,
                      allowed_symbols=frozenset({"US.AAPL"}))


def _engine(broker, cache_ticks, entry_price=200.0):
    strat = ThresholdStrategy(StrategyParams(symbol="US.AAPL", entry_price=entry_price,
                                             stop_loss_pct=0.05, take_profit_pct=0.10,
                                             confidence=0.7))
    return TradeEngine(broker=broker, strategy=strat, cfg=_cfg(), order_qty=1,
                       audit_path="/dev/null", snapshot_cache_ticks=cache_ticks)


def test_no_signal_ticks_share_one_snapshot(tmp_path):
    b = _CountingBroker(quotes={"US.AAPL": 100.0}, cash=10000.0)  # 100 < entry 200 -> NO_SIGNAL
    eng = _engine(b, cache_ticks=3)
    for _ in range(3):
        assert eng.tick().action == "NO_SIGNAL"
    assert b.account_calls == 1
    for _ in range(3):
        eng.tick()
    assert b.account_calls == 2


def test_default_is_fetch_per_tick(tmp_path):
    b = _CountingBroker(quotes={"US.AAPL": 100.0}, cash=10000.0)
    eng = _engine(b, cache_ticks=1)
    eng.tick(); eng.tick()
    assert b.account_calls == 2


def test_signal_routes_on_fresh_snapshot(tmp_path):
    b = _CountingBroker(quotes={"US.AAPL": 100.0}, cash=10000.0)
    eng = _engine(b, cache_ticks=5, entry_price=90.0)   # 100 >= 90 -> BUY
    assert eng.tick().action == "ORDER_PLACED"
    # one cached-tick fetch + one fresh pre-routing fetch (+ the stop-attach
    # refetch inside _attach_trailing_stop only when trailing_stop_pct > 0)
    assert b.account_calls == 2
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_snapshot_cache.py -q`
Expected: FAIL — `TradeEngine.__init__() got an unexpected keyword argument 'snapshot_cache_ticks'`.

- [ ] **Step 3: Implement**

`TradeEngine.__init__`: add param `snapshot_cache_ticks: int = 1` (after `session_id`) and:

```python
        # Refresh-token budgeting: the tick's account snapshot may be reused for
        # up to snapshot_cache_ticks ticks. get_account costs 2 refresh tokens
        # (accinfo + positions) against Moomoo's 10-per-30s budget, and a 5s loop
        # fetching every tick starves hedge-confirm/escalation queries. Cached
        # data feeds ONLY the strategy evaluate; routing always re-fetches.
        self._snap_cache_ticks = max(1, int(snapshot_cache_ticks))
        self._snap_cache = None
        self._snap_cache_left = 0
```

Add helper + rewire `tick`:

```python
    def _account_for_tick(self):
        if self._snap_cache is None or self._snap_cache_left <= 0:
            self._snap_cache = self._b.get_account()
            self._snap_cache_left = self._snap_cache_ticks
        self._snap_cache_left -= 1
        return self._snap_cache

    def tick(self) -> TickResult:
        if self._gate is not None and self._gate.halted:
            return TickResult("HALTED")
        snap = self._account_for_tick()
        symbol = self._strat.p.symbol
        price = self._b.get_quote(symbol)
        if price is None:
            return TickResult("NO_QUOTE", symbol)

        pos = next((p for p in snap.positions if p.symbol == symbol), None)
        signal = self._strat.evaluate(price=price, position=pos)
        if signal is None:
            return TickResult("NO_SIGNAL")
        # Routing decisions never run on cached data: drop the cache and
        # re-fetch fresh so the risk core sees current positions/exposure.
        self._snap_cache = None
        snap = self._b.get_account()
        return self._route_signal(signal, snap, price)
```

Also invalidate at the top of `submit_external_signal` (external routes change positions the cached tick would not see):

```python
    def submit_external_signal(self, signal: Signal) -> TickResult:
        self._snap_cache = None   # external routes invalidate the tick cache
        snap = self._b.get_account()
        ...
```

- [ ] **Step 4: Run tests**

Run: `python -m pytest tests/test_snapshot_cache.py -q && python -m pytest -q`
Expected: all green (default `1` + invalidate-on-route keeps every existing test byte-identical in behavior).

- [ ] **Step 5: Commit**

```bash
git add autotrader/main.py tests/test_snapshot_cache.py
git commit -m "feat(engine): snapshot cache for refresh-token budgeting (routing always fresh)

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 5: StopManager — morning stop reconciliation (fixes the Critical)

**Files:**
- Create: `autotrader/stops.py`
- Modify: `autotrader/domain.py` (add `is_option_symbol`), `autotrader/db.py` (add `get_trade_by_broker_order_id`), `autotrader/main.py` (public `attach_trailing_stop`, `_attach_trailing_stop` returns bool)
- Test: `tests/test_stop_manager.py` (create)

**Interfaces:**
- Consumes: `Broker.get_open_orders() -> Optional[List[OrderAck]]`, `DB.get_open_trailing_stop(symbol)`, `DB.mark_order_cancelled(boid)`, engine's audited stop path.
- Produces:
  - `domain.is_option_symbol(symbol: str) -> bool`
  - `DB.get_trade_by_broker_order_id(broker_order_id: str) -> Optional[tuple]` returning `(symbol, side, order_type)`
  - `TradeEngine.attach_trailing_stop(symbol: str, qty: int, ref_price: float, tag: str) -> bool` (True = stop placed)
  - `StopManager(engine, broker, db, cfg, alert_fn: Optional[Callable[[str], None]] = None)` with `reconcile(today: date) -> Optional[StopReconcileResult]`; `StopReconcileResult(attached, attach_failed, orphans_cancelled, already_protected, skipped_no_quote)`. Task 6 wires it at ENTRY_OPEN.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_stop_manager.py`:

```python
"""StopManager (spec W1): every held long must have exactly one working
trailing stop each morning; orphaned orders (stops for closed positions,
leftover option legs) are cancelled. Unknown broker state -> logged no-op."""
from datetime import date

from autotrader.config import RiskConfig
from autotrader.db import DB
from autotrader.domain import OrderRequest
from autotrader.main import TradeEngine
from autotrader.sim_broker import SimBroker
from autotrader.stops import StopManager
from autotrader.strategies.threshold import ThresholdStrategy, StrategyParams

TODAY = date(2026, 7, 6)


def _cfg(**over):
    base = dict(trading_env="PAPER", min_confidence=0.6, max_order_notional=50000,
                max_position_qty=1000, daily_loss_limit=500, max_gross_exposure=1000000,
                allowed_symbols=frozenset({"US.AAPL"}), trailing_stop_pct=5.0)
    base.update(over)
    return RiskConfig(**base)


def _mgr(tmp_path, broker, cfg=None, alert_fn=None):
    cfg = cfg or _cfg()
    db = DB(str(tmp_path / "sm.db"))
    strat = ThresholdStrategy(StrategyParams(symbol="US.AAPL", entry_price=1e9,
                                             stop_loss_pct=0.05, take_profit_pct=0.10,
                                             confidence=0.7))
    eng = TradeEngine(broker=broker, strategy=strat, cfg=cfg, order_qty=1,
                      audit_path=str(tmp_path / "audit.jsonl"), db=db)
    return StopManager(eng, broker, db, cfg, alert_fn=alert_fn), db, eng


def _seed_long(broker, qty=10):
    broker.place_order(OrderRequest(symbol="US.AAPL", side="BUY", qty=qty,
                                    order_type="MARKET", limit_price=None,
                                    client_order_id="seed"))


def test_unprotected_long_gets_stop_reattached(tmp_path):
    b = SimBroker(quotes={"US.AAPL": 100.0}, cash=100000.0)
    _seed_long(b)
    mgr, db, _ = _mgr(tmp_path, b)
    res = mgr.reconcile(TODAY)
    assert res.attached == 1 and res.orphans_cancelled == 0
    open_orders = b.get_open_orders()
    assert len(open_orders) == 1                       # the resting TRAILING_STOP
    assert db.get_open_trailing_stop("US.AAPL") is not None
    db.close()


def test_reconcile_is_idempotent_same_day(tmp_path):
    b = SimBroker(quotes={"US.AAPL": 100.0}, cash=100000.0)
    _seed_long(b)
    mgr, db, _ = _mgr(tmp_path, b)
    mgr.reconcile(TODAY)
    res2 = mgr.reconcile(TODAY)
    assert res2.attached == 0 and res2.already_protected == 1
    assert len(b.get_open_orders()) == 1               # still exactly one stop
    db.close()


def test_orphaned_stop_for_closed_position_is_cancelled(tmp_path):
    b = SimBroker(quotes={"US.AAPL": 100.0}, cash=100000.0)
    _seed_long(b)
    mgr, db, eng = _mgr(tmp_path, b)
    mgr.reconcile(TODAY)                                # attaches the stop
    b.place_order(OrderRequest(symbol="US.AAPL", side="SELL", qty=10,
                               order_type="MARKET", limit_price=None,
                               client_order_id="flatten"))   # position now gone
    res = mgr.reconcile(TODAY)
    assert res.orphans_cancelled == 1 and res.attached == 0
    assert b.get_open_orders() == []
    assert db.get_open_trailing_stop("US.AAPL") is None
    db.close()


def test_orphaned_option_leg_is_cancelled(tmp_path):
    opt = "US.AAPL260918C110000"
    b = SimBroker(quotes={"US.AAPL": 100.0, opt: 5.0}, cash=100000.0)
    _seed_long(b)
    # a leftover option-leg LIMIT resting from yesterday (non-marketable -> rests)
    ack = b.place_order(OrderRequest(symbol=opt, side="BUY", qty=1,
                                     order_type="LIMIT", limit_price=1.0,
                                     client_order_id="leg-old"))
    mgr, db, _ = _mgr(tmp_path, b)
    db.record_trade(client_order_id="leg-old", symbol=opt, side="BUY", qty=1,
                    order_type="LIMIT", limit_price=1.0,
                    broker_order_id=ack.broker_order_id, state="SUBMITTED")
    res = mgr.reconcile(TODAY)
    assert res.orphans_cancelled == 1
    assert res.attached == 1                            # the AAPL long still gets its stop
    working = {a.broker_order_id for a in b.get_open_orders()}
    assert ack.broker_order_id not in working
    db.close()


def test_unknown_book_is_a_noop(tmp_path):
    b = SimBroker(quotes={"US.AAPL": 100.0}, cash=100000.0)
    _seed_long(b)
    b.fail_open_orders = True
    mgr, db, _ = _mgr(tmp_path, b)
    assert mgr.reconcile(TODAY) is None                 # no attach, no cancel
    assert len(b._open) == 0
    db.close()


def test_rejected_attach_alerts_operator(tmp_path):
    alerts = []
    b = SimBroker(quotes={"US.AAPL": 100.0}, cash=100000.0)
    _seed_long(b)
    # symbol NOT in the allow-list -> risk core rejects the stop -> alert
    mgr, db, _ = _mgr(tmp_path, b, cfg=_cfg(allowed_symbols=frozenset({"US.MSFT"})),
                      alert_fn=alerts.append)
    res = mgr.reconcile(TODAY)
    assert res.attach_failed == 1 and res.attached == 0
    assert len(alerts) == 1 and "US.AAPL" in alerts[0]
    db.close()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_stop_manager.py -q`
Expected: FAIL with `ModuleNotFoundError: autotrader.stops`.

- [ ] **Step 3: Implement the supporting pieces**

`autotrader/domain.py` — add near `_OPT_SUFFIX_RE`:

```python
_OPT_CODE_TAIL_RE = re.compile(r"\d{6}[CP]\d+$")


def is_option_symbol(symbol: str) -> bool:
    """True for moomoo option codes (e.g. US.AAPL260717C210000): equity codes
    never end with the 6-digit-date + C/P + strike tail. Used to separate
    option positions (overlay-managed, O4) from equity longs (stop-managed)."""
    return _OPT_CODE_TAIL_RE.search(symbol) is not None
```

`autotrader/db.py` — add after `get_open_trailing_stop`:

```python
    def get_trade_by_broker_order_id(self, broker_order_id: str):
        """(symbol, side, order_type) of the newest trades row carrying this
        broker id, or None if the order is not in the projection (not ours)."""
        with self._lock:
            row = self._conn.execute(
                "SELECT symbol, side, order_type FROM trades "
                "WHERE broker_order_id=? ORDER BY id DESC LIMIT 1",
                (broker_order_id,)).fetchone()
        return row
```

`autotrader/main.py` — make `_attach_trailing_stop` report its outcome and add the public entry point (keep the private name; internal call sites at `main.py:240` and `:474` ignore the return value):

```python
    def attach_trailing_stop(self, symbol: str, qty: int, ref_price: float,
                             tag: str) -> bool:
        """Public audited stop-attach (StopManager's morning re-attach). Same
        path as entry-time attachment: risk core -> router -> DB. `tag` seeds
        the client_order_id, so one (tag, symbol, qty) attaches at most once."""
        return self._attach_trailing_stop(symbol, qty, ref_price, tag)
```

and change `_attach_trailing_stop`'s returns: `return False` at the risk-rejected branch, `return True` after the successful `record_trade`/log (signature becomes `-> bool`).

- [ ] **Step 4: Implement StopManager**

Create `autotrader/stops.py`:

```python
"""Stop lifecycle as a first-class concern (spec W1).

EOD cancels every working order while positions carry overnight, and Moomoo
orders are DAY time-in-force anyway — so protective trailing stops MUST be
re-attached every morning or carried positions are unprotected from day 2
(review Critical #1). StopManager reconciles "intended protection" (every held
equity long has exactly one working trailing stop) against "working orders"
(broker truth): it cancels orphans first (stops for closed positions, leftover
option legs, anything unrecognized — this also closes the option-leg
orphan-rest pre-live blocker), then attaches missing stops through the
engine's audited path. Unknown broker state (failed open-orders or positions
query) makes the whole reconcile a logged no-op — never attach or cancel
against an unknown book."""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date
from typing import Callable, Optional

from autotrader.domain import BrokerError, is_option_symbol

logger = logging.getLogger("autotrader.stops")


@dataclass(frozen=True)
class StopReconcileResult:
    attached: int
    attach_failed: int
    orphans_cancelled: int
    already_protected: int
    skipped_no_quote: int


class StopManager:
    def __init__(self, engine, broker, db, cfg,
                 alert_fn: Optional[Callable[[str], None]] = None):
        self._engine = engine
        self._b = broker
        self._db = db
        self._cfg = cfg
        self._alert = alert_fn

    def reconcile(self, today: date) -> Optional[StopReconcileResult]:
        open_orders = self._b.get_open_orders()
        if open_orders is None:
            logger.warning("stop reconcile: open-orders query failed — no-op "
                           "(retries next lifecycle run)")
            return None
        snap = self._b.get_account()
        if not snap.positions_loaded or snap.stale:
            logger.warning("stop reconcile: positions unavailable/stale — no-op")
            return None
        held = {p.symbol: p.qty for p in snap.positions
                if p.qty > 0 and not is_option_symbol(p.symbol)}

        # Pass 1 — orphan sweep, BEFORE attaching (so a fresh stop is never
        # swept by its own reconcile): cancel every working order that is not
        # a recognized trailing stop protecting a held long.
        orphans = 0
        for ack in open_orders:
            boid = ack.broker_order_id
            if not boid:
                continue
            row = self._db.get_trade_by_broker_order_id(boid)
            keep = (row is not None and row[2] == "TRAILING_STOP"
                    and row[1] == "SELL" and row[0] in held)
            if keep:
                continue
            try:
                self._b.cancel_order(boid)
            except BrokerError as e:
                logger.error("stop reconcile: cancel orphan %s failed: %s", boid, e)
                continue
            self._db.mark_order_cancelled(boid)
            orphans += 1
            logger.warning("stop reconcile: cancelled orphan order %s (%s)",
                           boid, row if row else "not in trades projection")

        if self._cfg.trailing_stop_pct <= 0:
            logger.info("stop reconcile: trailing stops disabled — attach pass skipped")
            return StopReconcileResult(0, 0, orphans, 0, 0)

        # Pass 2 — attach: every held equity long gets one working stop.
        surviving = {a.broker_order_id for a in open_orders if a.broker_order_id}
        surviving -= {None}
        attached = failed = protected = skipped = 0
        for symbol in sorted(held):
            qty = held[symbol]
            existing = self._db.get_open_trailing_stop(symbol)
            if existing is not None and existing in surviving:
                protected += 1
                continue
            price = self._b.get_quote(symbol)
            if price is None:
                skipped += 1
                logger.warning("stop reconcile: no quote for %s — cannot attach", symbol)
                continue
            if self._engine.attach_trailing_stop(
                    symbol, qty, price, f"reattach-{today.isoformat()}"):
                attached += 1
            else:
                failed += 1
                msg = (f"⚠ UNPROTECTED — {symbol} long {qty} has no working "
                       f"trailing stop and the morning re-attach was rejected.")
                logger.error(msg)
                if self._alert is not None:
                    try:
                        self._alert(msg)
                    except Exception as e:  # alerting must never break the loop
                        logger.error("stop reconcile: alert failed: %s", e)
        logger.info("stop reconcile: attached=%d failed=%d orphans=%d "
                    "protected=%d skipped=%d", attached, failed, orphans,
                    protected, skipped)
        return StopReconcileResult(attached, failed, orphans, protected, skipped)
```

Note on the orphan sweep in `test_orphaned_stop_for_closed_position_is_cancelled`: the swept stop's boid was cancelled at the broker AND marked CANCELLED in the DB, so the attach pass's `get_open_trailing_stop` returns None for symbols no longer held — no dangling projection rows.

- [ ] **Step 5: Run tests**

Run: `python -m pytest tests/test_stop_manager.py -q && python -m pytest -q`
Expected: all green.

- [ ] **Step 6: Commit**

```bash
git add autotrader/stops.py autotrader/domain.py autotrader/db.py autotrader/main.py tests/test_stop_manager.py
git commit -m "feat(stops): StopManager reconciles protection vs working orders (Critical #1)

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 6: Wire StopManager at ENTRY_OPEN; rename EOD_FLATTEN

**Files:**
- Modify: `autotrader/scheduler.py` (rename + alias), `autotrader/runner.py` (ctor + ENTRY_OPEN/EOD branches), `autotrader/main.py:795-802` (`main()` wiring)
- Test: `tests/test_stop_manager.py` (wiring test appended)

**Interfaces:**
- Consumes: `StopManager.reconcile(today)` (Task 5).
- Produces: `SessionRunner(..., stop_manager=None)` ctor param; scheduler constant `EOD_CANCEL_ORDERS` (with `EOD_FLATTEN = EOD_CANCEL_ORDERS` back-compat alias). Reconcile runs at **ENTRY_OPEN**, not PRE_OPEN_SYNC — deliberate spec deviation: stops are placeable and quotes live only once the market is open, and PRE_OPEN_SYNC's `ground_truth_sync` (which refreshes the DB's stop view) has already run by then (scheduler fires jobs chronologically, including catch-up).

- [ ] **Step 1: Write the failing test**

Append to `tests/test_stop_manager.py`:

```python
def test_runner_reconciles_stops_at_entry_open(tmp_path):
    """Full-loop wiring: a carried position with no stop gets one when the
    ENTRY_OPEN job fires (before deferred entries flush)."""
    from datetime import datetime
    from zoneinfo import ZoneInfo
    from autotrader.clock import FixedClock
    from autotrader.lifecycle import EntryGate
    from autotrader.runner import SessionRunner
    from autotrader.scheduler import LifecycleScheduler
    from autotrader.watchdog import Watchdog

    b = SimBroker(quotes={"US.AAPL": 100.0}, cash=100000.0)
    _seed_long(b)
    mgr, db, eng = _mgr(tmp_path, b)
    gate = EntryGate(enabled=False)
    eng._gate = gate   # reuse the engine built by _mgr
    runner = SessionRunner(engine=eng, broker=b, db=db, gate=gate,
                           scheduler=LifecycleScheduler(),
                           watchdog=Watchdog(health_check=lambda: True,
                                             reconcile=lambda: None,
                                             sleep=lambda s: None),
                           clock=FixedClock(datetime(2026, 7, 6, 9, 46,
                                                     tzinfo=ZoneInfo("America/New_York"))),
                           sleep=lambda s: None, stop_manager=mgr)
    runner.run_once(datetime(2026, 7, 6, 9, 46, tzinfo=ZoneInfo("America/New_York")))
    assert db.get_open_trailing_stop("US.AAPL") is not None
    db.close()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_stop_manager.py -q`
Expected: FAIL — `SessionRunner.__init__() got an unexpected keyword argument 'stop_manager'`.

- [ ] **Step 3: Implement**

`autotrader/scheduler.py` — rename the constant honestly (`EOD_FLATTEN` never flattened positions; review Minor #10a):

```python
EOD_CANCEL_ORDERS = "EOD_FLATTEN"   # 16:15 — cancel-all working orders + commit perf.
                                    # Keeps the legacy string value so persisted
                                    # scheduler state and old logs stay readable.
EOD_FLATTEN = EOD_CANCEL_ORDERS     # deprecated alias — positions are NOT flattened
```

and use `EOD_CANCEL_ORDERS` in `_SCHEDULE`.

`autotrader/runner.py`:
- import `EOD_CANCEL_ORDERS` (keep working imports; drop `EOD_FLATTEN` usage);
- ctor: add `stop_manager=None` after `reporter=None`, store `self._stop_manager = stop_manager`;
- `_run_job` ENTRY_OPEN branch:

```python
        elif job == ENTRY_OPEN:
            self._gate.open()
            logger.info("ENTRY_OPEN: entries enabled")
            # W1: reconcile protective stops FIRST (EOD cancelled them; DAY TIF
            # would have lapsed them anyway), then replay deferred entries —
            # whose own stops attach at entry.
            if self._stop_manager is not None:
                self._stop_manager.reconcile(now.date())
            if self._engine is not None:
                self._engine.flush_deferred_entries()
```

- `EOD_FLATTEN` branch: rename match to `EOD_CANCEL_ORDERS` and update the log line:

```python
        elif job == EOD_CANCEL_ORDERS:
            self._gate.close()
            if self._broker is not None:
                self._broker.cancel_all()
                self._record_perf()
            logger.info("EOD_CANCEL_ORDERS: entries closed, working orders cancelled "
                        "(stops re-attach at next ENTRY_OPEN), performance committed")
```

`autotrader/main.py` `main()` — after `reporter` setup, before the `SessionRunner` construction:

```python
    from autotrader.stops import StopManager
    stop_alert = None
    if slack_url:
        stop_alert = lambda text: _post_slack(slack_url, {"text": text})
    stop_manager = StopManager(engine, broker, db, cfg, alert_fn=stop_alert)
```

and pass `stop_manager=stop_manager` to `SessionRunner(...)`.

- [ ] **Step 4: Run tests**

Run: `python -m pytest tests/test_stop_manager.py tests/test_runner.py -q && python -m pytest -q`
Expected: all green (the alias keeps existing `EOD_FLATTEN` imports working).

- [ ] **Step 5: Commit**

```bash
git add autotrader/scheduler.py autotrader/runner.py autotrader/main.py tests/test_stop_manager.py
git commit -m "feat(runner): morning stop reconcile at ENTRY_OPEN; rename EOD_FLATTEN -> EOD_CANCEL_ORDERS

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 7: Trading calendar (no weekend/holiday trading)

**Files:**
- Create: `autotrader/market_calendar.py`
- Modify: `autotrader/config.py` (holidays field + env), `autotrader/runner.py` (gate), `autotrader/main.py` (wiring)
- Test: `tests/test_market_calendar.py` (create), `tests/test_config.py` (holiday parsing)

**Interfaces:**
- Consumes: `Clock.now_est()` dates (runner already has them).
- Produces: `market_calendar.is_trading_day(d: date, holidays: FrozenSet[date] = frozenset()) -> bool`; `RiskConfig.market_holidays: FrozenSet[date]` (env `RISK_MARKET_HOLIDAYS`, comma-separated ISO dates, default = 2026 NYSE full-day holidays); `SessionRunner(..., trading_day_fn: Optional[Callable[[date], bool]] = None)` — `None` preserves today's always-on behavior for tests; `main()` wires the real predicate. `run_once` returns `"NON_TRADING_DAY"` and does nothing (no jobs, no tick, no inbox) on non-trading days.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_market_calendar.py`:

```python
"""Trading calendar (spec W3): weekends and configured holidays run nothing —
no lifecycle jobs, no tick, no inbox (review Important #4: Saturday BUYs off
Friday's close). Early-close support is a tracked PRE-LIVE follow-up."""
from datetime import date, datetime
from zoneinfo import ZoneInfo

from autotrader.market_calendar import is_trading_day

_NY = ZoneInfo("America/New_York")


def test_weekday_is_trading_day():
    assert is_trading_day(date(2026, 7, 6)) is True          # Monday


def test_weekend_is_not():
    assert is_trading_day(date(2026, 7, 4)) is False         # Saturday
    assert is_trading_day(date(2026, 7, 5)) is False         # Sunday


def test_configured_holiday_is_not():
    hol = frozenset({date(2026, 7, 3)})
    assert is_trading_day(date(2026, 7, 3), hol) is False    # July 4th observed
    assert is_trading_day(date(2026, 7, 2), hol) is True


def test_runner_skips_everything_on_non_trading_day(tmp_path):
    from autotrader.clock import FixedClock
    from autotrader.config import RiskConfig
    from autotrader.db import DB
    from autotrader.lifecycle import EntryGate
    from autotrader.main import TradeEngine
    from autotrader.runner import SessionRunner
    from autotrader.scheduler import LifecycleScheduler
    from autotrader.sim_broker import SimBroker
    from autotrader.strategies.threshold import ThresholdStrategy, StrategyParams
    from autotrader.watchdog import Watchdog

    b = SimBroker(quotes={"US.AAPL": 101.0}, cash=100000.0)
    db = DB(str(tmp_path / "cal.db"))
    gate = EntryGate(enabled=False)
    cfg = RiskConfig(trading_env="PAPER", min_confidence=0.6, max_order_notional=20000,
                     max_position_qty=100, daily_loss_limit=500,
                     max_gross_exposure=100000, allowed_symbols=frozenset({"US.AAPL"}))
    strat = ThresholdStrategy(StrategyParams(symbol="US.AAPL", entry_price=100.0,
                                             stop_loss_pct=0.05, take_profit_pct=0.10,
                                             confidence=0.7))
    eng = TradeEngine(broker=b, strategy=strat, cfg=cfg, order_qty=1,
                      audit_path=str(tmp_path / "audit.jsonl"), db=db, entry_gate=gate)
    runner = SessionRunner(engine=eng, broker=b, db=db, gate=gate,
                           scheduler=LifecycleScheduler(),
                           watchdog=Watchdog(health_check=lambda: True,
                                             reconcile=lambda: None, sleep=lambda s: None),
                           clock=FixedClock(datetime(2026, 7, 4, 9, 46, tzinfo=_NY)),
                           sleep=lambda s: None,
                           trading_day_fn=is_trading_day)
    # Saturday 09:46: ENTRY_OPEN would fire and the 101>=100 BUY would place.
    assert runner.run_once(datetime(2026, 7, 4, 9, 46, tzinfo=_NY)) == "NON_TRADING_DAY"
    assert gate.entries_enabled is False
    assert b.get_account().position_qty("US.AAPL") == 0
    db.close()
```

Append to `tests/test_config.py`:

```python
def test_market_holidays_default_and_override(monkeypatch):
    from datetime import date
    from autotrader.config import load_risk_config
    monkeypatch.delenv("RISK_MARKET_HOLIDAYS", raising=False)
    cfg = load_risk_config()
    assert date(2026, 12, 25) in cfg.market_holidays          # shipped default
    monkeypatch.setenv("RISK_MARKET_HOLIDAYS", "2027-01-01, 2027-07-05")
    cfg = load_risk_config()
    assert cfg.market_holidays == frozenset({date(2027, 1, 1), date(2027, 7, 5)})


def test_market_holidays_bad_date_raises(monkeypatch):
    import pytest
    from autotrader.config import load_risk_config
    monkeypatch.setenv("RISK_MARKET_HOLIDAYS", "2026-13-45")
    with pytest.raises(ValueError):
        load_risk_config()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_market_calendar.py tests/test_config.py -q`
Expected: FAIL — no `autotrader.market_calendar` module, no `market_holidays` field, no `trading_day_fn` kwarg.

- [ ] **Step 3: Implement**

Create `autotrader/market_calendar.py`:

```python
"""Pure trading-calendar predicate (spec W3). No SDK, no I/O, no clock — the
caller passes the date (from the injected America/New_York Clock) and the
holiday set (from RiskConfig.market_holidays). Early-close (half-day)
awareness is a tracked PRE-LIVE follow-up, deliberately not implemented here."""
from __future__ import annotations

from datetime import date
from typing import FrozenSet


def is_trading_day(d: date, holidays: FrozenSet[date] = frozenset()) -> bool:
    """True on weekdays that are not configured full-day market holidays."""
    return d.weekday() < 5 and d not in holidays
```

`autotrader/config.py`:
- `from datetime import date` at top;
- module constant + field + loader:

```python
# NYSE full-day holidays for 2026 — the shipped default for RISK_MARKET_HOLIDAYS.
# Operators must extend this via config each year (tracked in PRE-LIVE.md).
_DEFAULT_2026_NYSE_HOLIDAYS = ("2026-01-01,2026-01-19,2026-02-16,2026-04-03,"
                               "2026-05-25,2026-06-19,2026-07-03,2026-09-07,"
                               "2026-11-26,2026-12-25")
```

dataclass field (after `order_cap_ticks`): `market_holidays: FrozenSet[date] = frozenset()`
and in `load_risk_config()` (before the `return`):

```python
    raw_holidays = os.getenv("RISK_MARKET_HOLIDAYS", _DEFAULT_2026_NYSE_HOLIDAYS)
    try:
        holidays = frozenset(date.fromisoformat(s.strip())
                             for s in raw_holidays.split(",") if s.strip())
    except ValueError as e:
        raise ValueError(f"RISK_MARKET_HOLIDAYS contains an invalid ISO date: {e}")
```

passing `market_holidays=holidays` in the constructor call. Also add `escalation_dwell_seconds` here? No — that lands in Task 11; keep this task calendar-only.

`autotrader/runner.py`:
- ctor: add `trading_day_fn=None` after `stop_manager=None`; store it;
- top of `run_once`:

```python
    def run_once(self, now) -> str:
        """... (append to docstring:) On a non-trading day (weekend/holiday,
        when a trading_day_fn is wired) NOTHING runs — no lifecycle jobs, no
        tick, no inbox — and 'NON_TRADING_DAY' is returned."""
        if self._trading_day_fn is not None and not self._trading_day_fn(now.date()):
            return "NON_TRADING_DAY"
```

`autotrader/main.py` `main()` — wire it:

```python
    from autotrader.market_calendar import is_trading_day
    runner = SessionRunner(
        ...,
        trading_day_fn=lambda d: is_trading_day(d, cfg.market_holidays),
    )
```

- [ ] **Step 4: Run tests**

Run: `python -m pytest tests/test_market_calendar.py tests/test_config.py -q && python -m pytest -q`
Expected: all green (`trading_day_fn=None` default leaves every existing runner test untouched).

- [ ] **Step 5: Commit**

```bash
git add autotrader/market_calendar.py autotrader/config.py autotrader/runner.py autotrader/main.py tests/test_market_calendar.py tests/test_config.py
git commit -m "feat(calendar): weekend/holiday gate for the whole session loop

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 8: DB `engine_state` table

**Files:**
- Modify: `autotrader/db.py` (schema + 4 methods)
- Test: `tests/test_db.py`

**Interfaces:**
- Produces: `DB.set_state(key: str, value: str)`, `DB.get_state(key: str) -> Optional[str]`, `DB.delete_state(key: str)`, `DB.list_state(prefix: str) -> List[Tuple[str, str]]`. Tasks 9 and 10 consume these.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_db.py`:

```python
def test_engine_state_roundtrip(tmp_path):
    from autotrader.db import DB
    db = DB(str(tmp_path / "s.db"))
    assert db.get_state("sched:EOD_REPORT") is None
    db.set_state("sched:EOD_REPORT", "2026-07-06")
    db.set_state("deferred:US.AAPL:BUY", "{}")
    assert db.get_state("sched:EOD_REPORT") == "2026-07-06"
    db.set_state("sched:EOD_REPORT", "2026-07-07")           # upsert
    assert db.get_state("sched:EOD_REPORT") == "2026-07-07"
    assert db.list_state("deferred:") == [("deferred:US.AAPL:BUY", "{}")]
    db.delete_state("deferred:US.AAPL:BUY")
    assert db.list_state("deferred:") == []
    db.close()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_db.py -q`
Expected: FAIL — `AttributeError: 'DB' object has no attribute 'get_state'`.

- [ ] **Step 3: Implement**

Append to `_SCHEMA` in `autotrader/db.py`:

```sql
CREATE TABLE IF NOT EXISTS engine_state (
    key        TEXT PRIMARY KEY,   -- namespaced: 'sched:<job>' / 'deferred:<sym>:<dir>'
    value      TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
```

Add methods (after `mark_order_cancelled`):

```python
    # --- engine_state: durable key/value for crash-safe engine state (W4) ----
    def set_state(self, key: str, value: str) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO engine_state (key,value,updated_at) "
                "VALUES (?,?,?)", (key, value, _now()))
            self._conn.commit()

    def get_state(self, key: str) -> Optional[str]:
        with self._lock:
            row = self._conn.execute(
                "SELECT value FROM engine_state WHERE key=?", (key,)).fetchone()
        return row[0] if row else None

    def delete_state(self, key: str) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM engine_state WHERE key=?", (key,))
            self._conn.commit()

    def list_state(self, prefix: str) -> List[tuple]:
        """[(key, value)] for keys starting with prefix, key-ordered."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT key, value FROM engine_state WHERE key LIKE ? ORDER BY key",
                (prefix + "%",)).fetchall()
        return rows
```

- [ ] **Step 4: Run tests**

Run: `python -m pytest tests/test_db.py -q && python -m pytest -q`
Expected: all green.

- [ ] **Step 5: Commit**

```bash
git add autotrader/db.py tests/test_db.py
git commit -m "feat(db): engine_state key/value table for durable engine state

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 9: Deferred entries survive restarts and expire same-day

**Files:**
- Modify: `autotrader/main.py:98-161` (`__init__` deferral state, `_defer_entry`, `flush_deferred_entries`, new `_restore_deferred_entries`)
- Test: `tests/test_deferred_entries.py`

**Interfaces:**
- Consumes: `DB.set_state/get_state/delete_state/list_state` (Task 8), `self._today_fn`.
- Produces: `self._deferred_entries: List[Tuple[str, Signal]]` — `(deferred_on ISO date, signal)`. DB keys `deferred:{symbol}:{direction}` holding JSON `{symbol, direction, confidence, rationale, stop_price, deferred_on}`. Same-day-only replay; stale rows expire with a warning log.

- [ ] **Step 1: Write the failing tests**

`tests/test_deferred_entries.py` already has `_cfg()`, `_engine(broker, gate, cfg=None, qty=10, tmp_path=None)` and `_buy()` helpers. First extend `_engine` to thread the new dependencies (existing callers are unaffected by the defaults):

```python
def _engine(broker, gate, cfg=None, qty=10, tmp_path=None, db=None, today=None):
    strat = ThresholdStrategy(StrategyParams(symbol="US.AAPL", entry_price=100.0,
                                             stop_loss_pct=0.05, take_profit_pct=0.10,
                                             confidence=0.7))
    return TradeEngine(broker=broker, strategy=strat, cfg=cfg or _cfg(),
                       order_qty=qty, audit_path=str(tmp_path / "audit.jsonl"),
                       entry_gate=gate, db=db,
                       today_fn=(lambda: today) if today else None)
```

Then append:

```python
def test_deferred_entry_survives_restart(tmp_path):
    """Crash between the pre-market defer and the 09:45 flush must not lose
    the signal (review Important #6a) — the deferral is persisted and a NEW
    engine on the same DB replays it when entries open."""
    from datetime import date
    from autotrader.db import DB
    db = DB(str(tmp_path / "d.db"))
    b = SimBroker(quotes={"US.AAPL": 101.0}, cash=100000.0)
    eng1 = _engine(b, EntryGate(enabled=False), tmp_path=tmp_path,
                   db=db, today=date(2026, 7, 6))
    assert eng1.submit_external_signal(_buy()).action == "ENTRY_DEFERRED"

    # "restart": a fresh engine on the same DB restores the deferral
    gate2 = EntryGate(enabled=False)
    eng2 = _engine(b, gate2, tmp_path=tmp_path, db=db, today=date(2026, 7, 6))
    gate2.open()
    results = eng2.flush_deferred_entries()
    assert [r.action for r in results] == ["ORDER_PLACED"]
    assert b.get_account().position_qty("US.AAPL") == 10
    assert db.list_state("deferred:") == []            # consumed
    db.close()


def test_stale_deferred_entry_expires_not_routed(tmp_path):
    """A deferral from a previous session date must NOT replay ~18h stale at
    the next open (review Important #6b) — it expires: logged, deleted, never
    routed."""
    from datetime import date
    from autotrader.db import DB
    db = DB(str(tmp_path / "d.db"))
    b = SimBroker(quotes={"US.AAPL": 101.0}, cash=100000.0)
    eng1 = _engine(b, EntryGate(enabled=False), tmp_path=tmp_path,
                   db=db, today=date(2026, 7, 6))
    assert eng1.submit_external_signal(_buy()).action == "ENTRY_DEFERRED"

    # next day: the restore path expires the persisted row
    gate2 = EntryGate(enabled=False)
    eng2 = _engine(b, gate2, tmp_path=tmp_path, db=db, today=date(2026, 7, 7))
    gate2.open()
    assert eng2.flush_deferred_entries() == []
    assert db.list_state("deferred:") == []            # expired + deleted
    assert b.get_account().position_qty("US.AAPL") == 0
    db.close()
```

Two EXISTING tests in this file reach into `engine._deferred_entries` with the old `List[Signal]` shape — update them in the same commit:
- `test_redeferring_same_symbol_keeps_only_the_latest`: change the last line to `assert eng._deferred_entries[0][1].confidence == 0.95`.
- (`test_external_buy_is_deferred_not_dropped_when_entries_closed` and `test_flush_is_a_noop_while_entries_still_closed` only assert `len(...)`, which still holds.)

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_deferred_entries.py -q`
Expected: the restart test FAILS (fresh engine has an empty in-memory list).

- [ ] **Step 3: Implement**

In `TradeEngine.__init__`, change the deferral state and restore (keep the existing explanatory comment, extend it):

```python
        # ... existing comment, then:
        # (deferred_on ISO date, Signal). Persisted to DB.engine_state (W4) so a
        # crash between a pre-market defer and the 09:45 flush cannot lose the
        # signal; rows from an earlier session date EXPIRE rather than replaying
        # ~18h stale.
        self._deferred_entries: "List[Tuple[str, Signal]]" = []
        if self._db is not None:
            self._restore_deferred_entries()
```

(`self._db` and `self._today_fn` are assigned earlier in `__init__` — keep this block after both.) Add:

```python
    def _restore_deferred_entries(self) -> None:
        import json
        today = self._today_fn().isoformat()
        for key, raw in self._db.list_state("deferred:"):
            try:
                d = json.loads(raw)
            except ValueError:
                logger.error("deferred entry %s: unreadable payload — dropped", key)
                self._db.delete_state(key)
                continue
            if d.get("deferred_on") != today:
                logger.warning("deferred entry %s expired (deferred_on=%s, today=%s) "
                               "— dropped, not routed", key, d.get("deferred_on"), today)
                self._db.delete_state(key)
                continue
            self._deferred_entries.append((d["deferred_on"], Signal(
                symbol=d["symbol"], direction=d["direction"],
                confidence=d["confidence"], rationale=d["rationale"],
                stop_price=d.get("stop_price"))))
```

Rewrite `_defer_entry` and `flush_deferred_entries`:

```python
    def _defer_entry(self, signal: Signal) -> None:
        """Queue an external entry for the next open, latest-wins per (symbol,
        direction), persisted so a restart cannot lose it (W4)."""
        deferred_on = self._today_fn().isoformat()
        self._deferred_entries = [
            e for e in self._deferred_entries
            if (e[1].symbol, e[1].direction) != (signal.symbol, signal.direction)
        ]
        self._deferred_entries.append((deferred_on, signal))
        if self._db is not None:
            import json
            self._db.set_state(
                f"deferred:{signal.symbol}:{signal.direction}",
                json.dumps({"symbol": signal.symbol, "direction": signal.direction,
                            "confidence": signal.confidence,
                            "rationale": signal.rationale,
                            "stop_price": signal.stop_price,
                            "deferred_on": deferred_on}))

    def flush_deferred_entries(self) -> "List[TickResult]":
        """Replay entries deferred while the window was closed. No-op unless
        entries are now open. Each is re-routed through submit_external_signal
        (re-sized, re-risk-checked against CURRENT data). Entries deferred on an
        EARLIER session date expire here instead of replaying stale."""
        if self._gate is None or not self._gate.entries_enabled:
            return []
        pending, self._deferred_entries = self._deferred_entries, []
        today = self._today_fn().isoformat()
        results: "List[TickResult]" = []
        for deferred_on, sig in pending:
            key = f"deferred:{sig.symbol}:{sig.direction}"
            if deferred_on != today:
                logger.warning("deferred %s %s expired (deferred_on=%s) — not routed",
                               sig.direction, sig.symbol, deferred_on)
                if self._db is not None:
                    self._db.delete_state(key)
                continue
            results.append(self.submit_external_signal(sig))
            if self._db is not None:
                self._db.delete_state(key)
        if pending:
            logger.info("flushed %d deferred entry(ies) at entry-open", len(results))
        return results
```

- [ ] **Step 4: Run tests**

Run: `python -m pytest tests/test_deferred_entries.py tests/test_main_loop.py tests/test_runner.py -q && python -m pytest -q`
Expected: all green (fix any test that reached into the old `List[Signal]` shape).

- [ ] **Step 5: Commit**

```bash
git add autotrader/main.py tests/test_deferred_entries.py
git commit -m "feat(engine): persist deferred entries; expire cross-session deferrals

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 10: Scheduler at-most-once jobs survive restarts

**Files:**
- Modify: `autotrader/scheduler.py`, `autotrader/main.py` (wiring)
- Test: `tests/test_scheduler.py`

**Interfaces:**
- Consumes: `DB.get_state`/`set_state` (Task 8), injected as plain callables (the scheduler stays DB-agnostic).
- Produces: `LifecycleScheduler(state_get=None, state_set=None)`; `AT_MOST_ONCE = frozenset({REBALANCE, EOD_CANCEL_ORDERS, EOD_REPORT})` persisted under `sched:{job}` keys. All other jobs deliberately re-fire on restart (catch-up = halt recovery).

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_scheduler.py`:

```python
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_scheduler.py -q`
Expected: FAIL — unexpected `state_get` kwarg.

- [ ] **Step 3: Implement**

`autotrader/scheduler.py`:

```python
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
```

(`EOD_CANCEL_ORDERS` exists from Task 6; note `AT_MOST_ONCE` must be defined after the constants.)

`autotrader/main.py` `main()`: `scheduler=LifecycleScheduler(state_get=db.get_state, state_set=db.set_state)`.

- [ ] **Step 4: Run tests**

Run: `python -m pytest tests/test_scheduler.py -q && python -m pytest -q`
Expected: all green (defaults `None` keep every existing scheduler use unchanged).

- [ ] **Step 5: Commit**

```bash
git add autotrader/scheduler.py autotrader/main.py tests/test_scheduler.py
git commit -m "feat(scheduler): persist at-most-once job state across restarts

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 11: Escalation dwell + cancel-race guard

**Files:**
- Modify: `autotrader/config.py` (`escalation_dwell_seconds`), `autotrader/main.py` (`__init__` + `_submit_with_escalation` + new `_cancel_for_escalation`)
- Test: `tests/test_limit_escalation.py`

**Interfaces:**
- Consumes: `_order_working` tri-state (Task 3).
- Produces: `RiskConfig.escalation_dwell_seconds: float = 20.0` (env `RISK_ESCALATION_DWELL_SECONDS`); `TradeEngine(..., escalation_sleep=None)` (no-op default; Task 15 wires `time.sleep`); `TradeEngine._cancel_for_escalation(ack) -> bool`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_limit_escalation.py`:

```python
def test_escalation_dwells_between_stages(tmp_path):
    """Each stage gets escalation_dwell_seconds to fill before being judged
    resting (review Important #3: zero dwell degenerates the feature into
    'MARKET with extra API calls' on live)."""
    slept = []
    b = SimBroker(quotes={"US.AAPL": 100.0}, cash=1_000_000.0, spread_bps=100.0)
    strat = ThresholdStrategy(StrategyParams(symbol="US.AAPL", entry_price=100.0,
                                             stop_loss_pct=0.05, take_profit_pct=0.10,
                                             confidence=0.7))
    eng = TradeEngine(broker=b, strategy=strat,
                      cfg=_cfg(order_cap_bps=5.0, escalation_dwell_seconds=7.5),
                      order_qty=10, audit_path=str(tmp_path / "audit.jsonl"),
                      escalation_sleep=slept.append)
    assert eng.tick().action == "ORDER_PLACED"
    assert slept == [7.5, 7.5]          # stage-1 dwell + re-peg dwell before MARKET


def test_failed_cancel_never_double_submits(tmp_path):
    """cancel_order raising (commonly 'already filled') must ABORT escalation —
    submitting the next stage after a failed cancel risks a duplicate fill."""
    from autotrader.domain import BrokerError, BrokerErrorKind

    class _StickyCancel(SimBroker):
        def cancel_order(self, boid):
            raise BrokerError(BrokerErrorKind.UNKNOWN, "cancel rejected: already filled")

    b = _StickyCancel(quotes={"US.AAPL": 100.0}, cash=1_000_000.0, spread_bps=100.0)
    strat = ThresholdStrategy(StrategyParams(symbol="US.AAPL", entry_price=100.0,
                                             stop_loss_pct=0.05, take_profit_pct=0.10,
                                             confidence=0.7))
    eng = TradeEngine(broker=b, strategy=strat, cfg=_cfg(order_cap_bps=5.0),
                      order_qty=10, audit_path=str(tmp_path / "audit.jsonl"))
    assert eng.tick().action == "ORDER_PLACED"
    assert b.get_account().position_qty("US.AAPL") == 0   # no re-peg, no MARKET
    assert len(b._open) == 1                              # only the original resting limit
```

Also append to `tests/test_config.py`:

```python
def test_escalation_dwell_from_env(monkeypatch):
    from autotrader.config import load_risk_config
    monkeypatch.delenv("RISK_ESCALATION_DWELL_SECONDS", raising=False)
    assert load_risk_config().escalation_dwell_seconds == 20.0
    monkeypatch.setenv("RISK_ESCALATION_DWELL_SECONDS", "5")
    assert load_risk_config().escalation_dwell_seconds == 5.0
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_limit_escalation.py tests/test_config.py -q`
Expected: FAILs — no `escalation_dwell_seconds` field, no `escalation_sleep` kwarg, double-submit still occurs.

- [ ] **Step 3: Implement**

`autotrader/config.py`: field `escalation_dwell_seconds: float = 20.0` (with the other §3 limit-order fields) + loader line `escalation_dwell_seconds=_f("RISK_ESCALATION_DWELL_SECONDS", 20.0),`.

`autotrader/main.py` `__init__`: add param `escalation_sleep=None` (after `hedge_confirm_sleep`) and

```python
        # Injected dwell between escalation stages — same pattern as
        # hedge_confirm_sleep: no-op in tests, time.sleep in production wiring.
        self._escalation_sleep = escalation_sleep or (lambda _s: None)
```

Add the cancel guard and restructure `_submit_with_escalation` (final form, incorporating Task 3's tri-state):

```python
    def _cancel_for_escalation(self, ack) -> bool:
        """Cancel ack's resting order; True only when the cancel SUCCEEDED and
        the next escalation stage may submit. A cancel failure usually means
        'already filled' — submitting the next stage then would DOUBLE-FILL
        (review Important #3), so the caller must stop escalating instead. The
        abandoned resting/filled order is cleaned up by the EOD cancel-all and
        the morning stop reconcile."""
        if not ack.broker_order_id:
            return False
        try:
            self._b.cancel_order(ack.broker_order_id)
            return True
        except BrokerError as e:
            logger.warning("escalation: cancel %s failed (%s) — NOT advancing",
                           ack.broker_order_id, e)
            return False

    def _submit_with_escalation(self, req: OrderRequest, snap, ref_price: float):
        """Submit a capped LIMIT; give it escalation_dwell_seconds to fill; if
        still resting, cancel + re-peg once to the current touch; dwell again;
        if still resting, submit MARKET so a risk exit completes. Each stage
        re-runs the risk core and uses a distinct client_order_id. An UNKNOWN
        open-orders book or a FAILED cancel aborts escalation with the current
        ack — a duplicate fill is worse than a resting limit.

        Returns (ack, terminal_req): terminal_req produced the returned ack."""
        if req.order_type != "LIMIT":
            return self._router.submit(req), req
        ack = self._router.submit(req)
        ack_req = req

        self._escalation_sleep(self._cfg.escalation_dwell_seconds)
        working = self._order_working(ack)
        if working is None:
            logger.warning("escalation: open-orders unknown — leaving %s as-is",
                           ack.client_order_id)
            return ack, req
        if not working:
            return ack, req   # filled (or terminal) within the dwell
        if not self._cancel_for_escalation(ack):
            return ack, req

        # Stage 2: re-peg to the CURRENT touch (original order confirmed cancelled).
        from autotrader.limit_pricing import capped_limit_price
        cur = self._b.get_quote(req.symbol) or ref_price
        peg_cid = req.client_order_id + "-peg"
        peg = OrderRequest(symbol=req.symbol, side=req.side, qty=req.qty,
                           order_type="LIMIT",
                           limit_price=capped_limit_price(req.side, cur, self._cfg),
                           client_order_id=peg_cid, option=req.option,
                           position_effect=req.position_effect,
                           correlation_id=req.correlation_id)
        if evaluate(peg, snap, self._cfg, ref_price=cur).approved:
            ack = self._router.submit(peg)
            ack_req = peg
            self._escalation_sleep(self._cfg.escalation_dwell_seconds)
            working = self._order_working(ack)
            if working is None:
                logger.warning("escalation: open-orders unknown after re-peg — "
                               "leaving %s as-is", ack.client_order_id)
                return ack, peg
            if not working:
                return ack, peg
            if not self._cancel_for_escalation(ack):
                return ack, ack_req

        # Stage 3: MARKET fallback — reached only with no order left resting.
        mkt_cid = req.client_order_id + "-mkt"
        mkt = OrderRequest(symbol=req.symbol, side=req.side, qty=req.qty,
                           order_type="MARKET", limit_price=None,
                           client_order_id=mkt_cid, option=req.option,
                           position_effect=req.position_effect,
                           correlation_id=req.correlation_id)
        decision = evaluate(mkt, snap, self._cfg, ref_price=cur)
        if not decision.approved:
            logger.warning("escalation: MARKET fallback rejected by risk: %s",
                           decision.reason)
            return ack, ack_req
        return self._router.submit(mkt), mkt
```

(Behavior notes: SimBroker's `cancel_order` never raises, and the no-op sleep default keeps every existing escalation test green; the only semantic change existing tests could see is that a failed cancel now aborts — SimBroker never fails a cancel.)

- [ ] **Step 4: Run tests**

Run: `python -m pytest tests/test_limit_escalation.py tests/test_config.py -q && python -m pytest -q`
Expected: all green.

- [ ] **Step 5: Commit**

```bash
git add autotrader/config.py autotrader/main.py tests/test_limit_escalation.py tests/test_config.py
git commit -m "feat(engine): escalation dwell + cancel-race guard (no double-fill)

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 12: Touch-priced MARKET-stage risk check (`get_touch`)

**Files:**
- Modify: `autotrader/broker.py`, `autotrader/sim_broker.py`, `autotrader/moomoo_broker.py`, `autotrader/main.py` (stage 3 of `_submit_with_escalation`)
- Test: `tests/test_limit_escalation.py`, `tests/test_moomoo_broker_offline.py`

**Interfaces:**
- Produces: `Broker.get_touch(symbol) -> Optional[Tuple[float, float]]` — `(bid, ask)` or `None` when unavailable (caller falls back to the last quote). Stage-3 MARKET risk evaluation prices BUYs at the ask and SELLs at the bid, making the "risk vetoes MARKET fallback" branch reachable via configuration (restores the intent of the spec's T6b).

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_limit_escalation.py`:

```python
def test_market_stage_vetoed_on_touch_priced_notional(tmp_path):
    """T6b via configuration: with a 100bps spread the MARKET stage's ask-priced
    notional (10 x 100.5 = 1005) breaches a 1002 cap that the capped-limit
    stages (10 x 100.05 = 1000.5) do not — the risk core vetoes the MARKET
    fallback and the re-pegged limit is left resting."""
    b = SimBroker(quotes={"US.AAPL": 100.0}, cash=1_000_000.0, spread_bps=100.0)
    eng = _engine(b, _cfg(order_cap_bps=5.0, max_order_notional=1002.0), tmp_path)
    assert eng.tick().action == "ORDER_PLACED"
    assert b.get_account().position_qty("US.AAPL") == 0   # MARKET was vetoed
    assert len(b._open) == 1                              # re-pegged limit rests
```

Append to `tests/test_moomoo_broker_offline.py`:

```python
def test_get_touch_reads_bid_ask_from_snapshot():
    class _QuoteCtx:
        def get_market_snapshot(self, codes):
            return 0, _DF([_Row({"code": codes[0], "bid_price": 99.5, "ask_price": 100.5})])

    b = _broker_with_trade(_EmptyOrderListTrade())
    b._quote = _QuoteCtx()
    assert b.get_touch("US.AAPL") == (99.5, 100.5)


def test_get_touch_none_when_unavailable():
    class _FailQuoteCtx:
        def get_market_snapshot(self, codes):
            return -1, "err"

    b = _broker_with_trade(_EmptyOrderListTrade())
    b._quote = _FailQuoteCtx()
    assert b.get_touch("US.AAPL") is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_limit_escalation.py tests/test_moomoo_broker_offline.py -q`
Expected: FAILs — no `get_touch`; MARKET stage evaluated at the last price (1000 ≤ 1002) fills.

- [ ] **Step 3: Implement**

`autotrader/broker.py` (after `get_quote`):

```python
    def get_touch(self, symbol: str):
        """(bid, ask) for symbol, or None when unavailable. Base returns None
        so brokers without touch data degrade to last-quote pricing."""
        return None
```

`autotrader/sim_broker.py`:

```python
    def get_touch(self, symbol: str):
        if symbol not in self._quotes:
            return None
        return self._touch(symbol)
```

`autotrader/moomoo_broker.py` (after `get_quote`):

```python
    def get_touch(self, symbol: str):
        """(bid, ask) from a market snapshot, or None (quote failure / no book).
        Quote-context call — does not consume trade refresh tokens."""
        ret, data = self._quote.get_market_snapshot([symbol])
        if not self._ok(ret) or self._c.is_empty(data):
            return None
        row = data.iloc[0]
        bid = self._c.safe_float(self._c.safe_get(row, "bid_price", "bid", default=0))
        ask = self._c.safe_float(self._c.safe_get(row, "ask_price", "ask", default=0))
        if bid <= 0 or ask <= 0:
            return None
        return (bid, ask)
```

`autotrader/main.py` — in `_submit_with_escalation` stage 3, replace the `evaluate(mkt, ...)` line:

```python
        # Price the MARKET stage's risk check at the side it will actually
        # execute (BUY lifts the ask, SELL hits the bid) rather than the last
        # quote — this makes the veto branch reachable and honest (spec W5).
        touch = self._b.get_touch(req.symbol)
        if touch is not None:
            bid, ask = touch
            exec_ref = ask if req.side == "BUY" else bid
        else:
            exec_ref = cur
        decision = evaluate(mkt, snap, self._cfg, ref_price=exec_ref)
```

- [ ] **Step 4: Run tests**

Run: `python -m pytest tests/test_limit_escalation.py tests/test_moomoo_broker_offline.py -q && python -m pytest -q`
Expected: all green (existing escalation tests use huge notional caps, unaffected by ask-pricing).

- [ ] **Step 5: Commit**

```bash
git add autotrader/broker.py autotrader/sim_broker.py autotrader/moomoo_broker.py autotrader/main.py tests/test_limit_escalation.py tests/test_moomoo_broker_offline.py
git commit -m "feat(engine): touch-priced risk check for the MARKET escalation stage

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 13: Option CLOSE legs exempt from the entry premium budget

**Files:**
- Modify: `autotrader/risk_core.py:120-193` (`_evaluate_option_leg`)
- Test: `tests/test_risk_close_exemption.py` (create)

**Interfaces:**
- Consumes: existing `OrderRequest.position_effect` (`"OPEN"`/`"CLOSE"`), `OptionContract`.
- Produces: CLOSE legs skip the premium budget and absolute ceiling; env/stale/allow-list/premium-sanity/contract-cap checks still apply (spec W6). No signature changes.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_risk_close_exemption.py`:

```python
"""Spec W6: BUY-to-CLOSE / SELL-to-CLOSE option legs must never be blocked by
the ENTRY premium budget — mirroring the equity reduce-only exemption, the
risk system must never block its own exit (review Important #5)."""
from datetime import date

from autotrader.config import RiskConfig
from autotrader.domain import AccountSnapshot, OptionContract, OrderRequest
from autotrader.risk_core import evaluate


def _cfg():
    return RiskConfig(trading_env="PAPER", min_confidence=0.6,
                      max_order_notional=20000, max_position_qty=100,
                      daily_loss_limit=500, max_gross_exposure=100000,
                      allowed_symbols=frozenset({"US.AAPL"}),
                      max_option_contracts=5, option_max_risk_pct=0.02)


def _snap(stale=False):
    # NLV 10_000 -> premium budget = 200
    return AccountSnapshot(cash=10000.0, total_assets=10000.0, day_pnl=0.0,
                           stale=stale)


def _leg(side, effect, premium=5.0):
    opt = OptionContract(underlying="US.AAPL", expiry=date(2026, 9, 18),
                         strike=110.0, right="CALL",
                         code="US.AAPL260918C110000")
    return OrderRequest(symbol=opt.code, side=side, qty=1, order_type="LIMIT",
                        limit_price=premium, client_order_id="t",
                        option=opt, position_effect=effect)


def test_buy_to_close_above_budget_is_approved():
    # 1 contract x $5 x 100 = $500 premium > $200 budget — but it is an EXIT.
    d = evaluate(_leg("BUY", "CLOSE"), _snap(), _cfg(), ref_price=5.0)
    assert d.approved, d.reason


def test_sell_to_close_above_budget_is_approved():
    d = evaluate(_leg("SELL", "CLOSE"), _snap(), _cfg(), ref_price=5.0)
    assert d.approved, d.reason


def test_buy_to_open_above_budget_still_vetoed():
    d = evaluate(_leg("BUY", "OPEN"), _snap(), _cfg(), ref_price=5.0)
    assert not d.approved and "budget" in d.reason


def test_close_leg_still_refused_on_stale_snapshot():
    d = evaluate(_leg("BUY", "CLOSE"), _snap(stale=True), _cfg(), ref_price=5.0)
    assert not d.approved and "stale" in d.reason
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_risk_close_exemption.py -q`
Expected: the two CLOSE-approval tests FAIL ("budget" veto).

- [ ] **Step 3: Implement**

In `_evaluate_option_leg`, insert the exemption between the contracts-cap check and the credit/debit budget block, restructuring the branch:

```python
    if req.qty > cfg.max_option_contracts:
        return RiskDecision(False,
                            f"contracts {req.qty} > cap {cfg.max_option_contracts}")

    nlv = snapshot.total_assets
    budget = nlv * cfg.option_max_risk_pct
    abs_ceiling = cfg.max_option_premium_per_trade
    gross = req.qty * premium * opt.multiplier

    if req.position_effect == "CLOSE":
        # Exit legs (buy-to-close / sell-to-close) are NEVER premium-budget
        # gated — the mirror of the equity reduce-only exemption: closing an
        # option position cannot increase the premium at risk, and the risk
        # system must never block its own exit (spec W6). Env / stale /
        # allow-list / premium-sanity / contract-cap checks above still apply,
        # and the daily-loss guard below is OPEN-only already.
        return RiskDecision(True, "OK")

    if req.side == "SELL":
        # SELL + OPEN (credit): defined-risk coverage + 2x-premium cap.
        ...  # existing SELL+OPEN block unchanged
    else:
        # BUY + OPEN (debit): max loss = 100% of premium paid.
        ...  # existing else block unchanged
```

(The old condition `req.side == "SELL" and req.position_effect == "OPEN"` simplifies to `req.side == "SELL"` because CLOSE already returned.)

- [ ] **Step 4: Run tests**

Run: `python -m pytest tests/test_risk_close_exemption.py -q && python -m pytest -q`
Expected: all green (no existing REGISTRY leg uses CLOSE, so options tests are unaffected).

- [ ] **Step 5: Commit**

```bash
git add autotrader/risk_core.py tests/test_risk_close_exemption.py
git commit -m "fix(risk): exempt option CLOSE legs from the entry premium budget

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 14: Runner is the sole performance writer

**Files:**
- Modify: `autotrader/main.py:177-184` (delete block in `_route_signal`), `:684-688` (delete block in `apply_risk_check`), `autotrader/runner.py:107-108` (RISK_CHECK branch)
- Test: `tests/test_runner.py` (new test), plus updates to any test asserting the old engine writes (expect them in `tests/test_main_loop.py` / `tests/test_engine_risk_check.py`)

**Interfaces:**
- Produces: the ONLY `record_performance` call sites are `SessionRunner._record_perf` (RISK_SWEEP, EOD_CANCEL_ORDERS, and now RISK_CHECK_MID/LATE). Engine signal/risk paths no longer write the row (review Important #7: a post-16:15 stop SELL overwrote the authoritative EOD numbers with raw broker figures).

- [ ] **Step 1: Write the failing test**

Append to `tests/test_runner.py` (uses its `_build`/`_dt` helpers):

```python
def test_engine_routes_never_overwrite_performance_row(tmp_path):
    """W7: the runner's fills-derived EOD write is authoritative; a signal
    routed afterwards (e.g. a post-16:15 stop SELL — SELLs are never gated)
    must not replace it with raw broker day_pnl."""
    b = SimBroker(quotes={"US.AAPL": 101.0}, cash=100000.0)
    runner, db, gate = _build(tmp_path, b)
    runner.run_once(_dt(9, 46))            # ENTRY_OPEN + BUY placed
    runner.run_once(_dt(16, 16))           # EOD job: authoritative perf write
    before = db._conn.execute(
        "SELECT day_pnl, updated_at FROM performance").fetchone()
    # a SELL routed after the EOD write (engine path)
    from autotrader.domain import Signal
    runner._engine.submit_external_signal(
        Signal(symbol="US.AAPL", direction="SELL", confidence=0.9, rationale="stop"))
    after = db._conn.execute(
        "SELECT day_pnl, updated_at FROM performance").fetchone()
    assert after == before                 # row untouched by the engine route
    db.close()


def test_risk_check_jobs_record_perf_via_runner(tmp_path):
    b = SimBroker(quotes={"US.AAPL": 90.0}, cash=100000.0)   # 90 < 100: NO_SIGNAL
    runner, db, gate = _build(tmp_path, b)
    runner.run_once(_dt(13, 31))           # RISK_CHECK_MID
    row = db._conn.execute("SELECT COUNT(*) FROM performance").fetchone()
    assert row[0] == 1                     # runner wrote it (engine no longer does)
    db.close()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/test_runner.py -q`
Expected: first test FAILS (`after != before` — the engine overwrote the row).

- [ ] **Step 3: Implement**

`autotrader/main.py`:
- In `_route_signal`, delete the whole `if self._db: self._db.record_performance(...)` block (`main.py:177-184`). Leave `record_signal` and everything else.
- In `apply_risk_check`, delete the `record_performance` block (`main.py:684-688`), keeping `risk_evaluate` and the GATE/HALT handling. Add a comment where it was:

```python
        # W7: performance rows are written ONLY by SessionRunner._record_perf
        # (fills-derived realized, quote-based unrealized) — never from engine
        # paths with raw broker figures the EOD report distrusts.
```

`autotrader/runner.py` RISK_CHECK branch:

```python
        elif job in (RISK_CHECK_MID, RISK_CHECK_LATE):
            self._engine.apply_risk_check(now)
            # W7: the runner is the sole performance writer.
            if self._broker is not None and self._db is not None:
                self._record_perf()
```

- [ ] **Step 4: Run the suite and update displaced assertions**

Run: `python -m pytest -q`
Expected failures: only tests that asserted the ENGINE wrote performance rows (look in `tests/test_main_loop.py` and `tests/test_engine_risk_check.py` for `performance` queries after `tick()`/`apply_risk_check()`). Update each to the new invariant: engine paths write **no** performance row; risk-check/EOD *jobs via the runner* do. Do not weaken any other assertion.

Run again: `python -m pytest -q` → all green.

- [ ] **Step 5: Commit**

```bash
git add autotrader/main.py autotrader/runner.py tests/test_runner.py tests/test_main_loop.py tests/test_engine_risk_check.py
git commit -m "fix(reporting): runner is the sole performance-row writer (W7)

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 15: `build_engine` factory — production wiring is testable

**Files:**
- Modify: `autotrader/main.py` (new `build_engine`, `main()` uses it)
- Test: `tests/test_build_engine.py` (create)

**Interfaces:**
- Produces: `build_engine(broker, strategy, cfg, *, order_qty, audit_path, db=None, entry_gate=None, alert_url=None) -> TradeEngine` with `hedge_confirm_sleep=time.sleep`, `escalation_sleep=time.sleep`, `snapshot_cache_ticks` from `AUTOTRADER_SNAPSHOT_CACHE_TICKS` (default 6). Fixes review Important #2 (`hedge_confirm_sleep` was never wired, so the live fill-poll was three instantaneous checks).

- [ ] **Step 1: Write the failing test**

Create `tests/test_build_engine.py`:

```python
"""build_engine is the production TradeEngine wiring — the one place that must
pass real sleeps (review Important #2: main() omitted hedge_confirm_sleep, so
the 'bounded live fill-poll' waited ~0ms) and the snapshot-cache size."""
import time

from autotrader.config import RiskConfig
from autotrader.main import build_engine
from autotrader.sim_broker import SimBroker
from autotrader.strategies.threshold import ThresholdStrategy, StrategyParams


def _parts(tmp_path):
    cfg = RiskConfig(trading_env="PAPER", min_confidence=0.6, max_order_notional=2000,
                     max_position_qty=100, daily_loss_limit=500,
                     max_gross_exposure=50000, allowed_symbols=frozenset({"US.AAPL"}))
    strat = ThresholdStrategy(StrategyParams(symbol="US.AAPL", entry_price=100.0,
                                             stop_loss_pct=0.05, take_profit_pct=0.10,
                                             confidence=0.7))
    return SimBroker(quotes={"US.AAPL": 90.0}), strat, cfg


def test_build_engine_wires_real_sleeps(tmp_path):
    b, strat, cfg = _parts(tmp_path)
    eng = build_engine(b, strat, cfg, order_qty=1,
                       audit_path=str(tmp_path / "a.jsonl"))
    assert eng._hedge_confirm_sleep is time.sleep
    assert eng._escalation_sleep is time.sleep


def test_build_engine_snapshot_cache_from_env(tmp_path, monkeypatch):
    b, strat, cfg = _parts(tmp_path)
    monkeypatch.delenv("AUTOTRADER_SNAPSHOT_CACHE_TICKS", raising=False)
    eng = build_engine(b, strat, cfg, order_qty=1,
                       audit_path=str(tmp_path / "a.jsonl"))
    assert eng._snap_cache_ticks == 6
    monkeypatch.setenv("AUTOTRADER_SNAPSHOT_CACHE_TICKS", "12")
    eng = build_engine(b, strat, cfg, order_qty=1,
                       audit_path=str(tmp_path / "a.jsonl"))
    assert eng._snap_cache_ticks == 12
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_build_engine.py -q`
Expected: FAIL — `ImportError: cannot import name 'build_engine'`.

- [ ] **Step 3: Implement**

Add to `autotrader/main.py` (module level, after `TradeEngine`):

```python
def build_engine(broker, strategy, cfg, *, order_qty: int, audit_path: str,
                 db=None, entry_gate=None, alert_url=None) -> TradeEngine:
    """Production TradeEngine wiring — the ONE place real time enters the
    engine: time.sleep for the hedge fill-poll and the escalation dwell (unit
    tests inject no-ops/recorders through the ctor instead), and the snapshot
    cache sized from AUTOTRADER_SNAPSHOT_CACHE_TICKS (default 6 ≈ 30s at the
    5s loop, keeping refresh-token use inside Moomoo's 10-per-30s budget)."""
    import os as _os
    import time as _time
    return TradeEngine(
        broker, strategy, cfg, order_qty=order_qty, audit_path=audit_path,
        db=db, entry_gate=entry_gate, alert_url=alert_url,
        hedge_confirm_sleep=_time.sleep,
        escalation_sleep=_time.sleep,
        snapshot_cache_ticks=int(_os.getenv("AUTOTRADER_SNAPSHOT_CACHE_TICKS", "6")),
    )
```

In `main()`, replace the `TradeEngine(...)` construction with:

```python
    engine = build_engine(broker, strat, cfg,
                          order_qty=int(os.getenv("ORDER_QTY", "1")),
                          audit_path=audit, db=db, entry_gate=gate,
                          alert_url=slack_url)
```

- [ ] **Step 4: Run tests**

Run: `python -m pytest tests/test_build_engine.py -q && python -m pytest -q`
Expected: all green.

- [ ] **Step 5: Commit**

```bash
git add autotrader/main.py tests/test_build_engine.py
git commit -m "fix(engine): build_engine factory wires real sleeps + snapshot cache (Important #2)

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 16: PRE-LIVE.md gate + CLAUDE.md env-var fix

**Files:**
- Create: `PRE-LIVE.md`
- Modify: `CLAUDE.md` (two `TRADING_ENV` mentions)

**Interfaces:** none (documentation gate).

- [ ] **Step 1: Create `PRE-LIVE.md`**

```markdown
# PRE-LIVE Gate

**Rule: no live flag flip (`RISK_TRADING_ENV=LIVE`, `RISK_LIMIT_ORDERS_ENABLED=1`,
`RISK_REBALANCE_ENABLED=1`) while ANY box below is open.** Source: 2026-07-02
full-codebase review + spec `docs/superpowers/specs/2026-07-02-pre-live-hardening-design.md`.

## Blocking — code (spec workstreams)

- [ ] W1 StopManager: morning stop re-attach + orphan sweep (review Critical #1;
      absorbs the option-leg orphan-rest blocker)
- [ ] W2 Fail-loud broker queries (None ≠ []) + conservative call sites +
      snapshot cache (review Important #1)
- [ ] W3 Trading calendar gates the whole loop (review Important #4)
- [ ] W4 Durable engine state: deferred entries + at-most-once scheduler jobs
      (review Important #6, Minor #8)
- [ ] W5 Escalation dwell + cancel-race guard + touch-priced MARKET risk check
      (review Important #3, T6b)
- [ ] W6 Option CLOSE legs exempt from the entry premium budget (review Important #5)
- [ ] W7 Runner is the sole performance writer (review Important #7)
- [ ] W8 build_engine wires hedge_confirm_sleep/escalation_sleep = time.sleep
      (review Important #2)

## Blocking — operational

- [ ] The paper-only guards are consciously revised for the live cutover:
      `main()` refuses `trading_env != "PAPER"` (autotrader/main.py) and the
      risk core rejects non-PAPER envs (autotrader/risk_core.py). Both must be
      changed deliberately, with human review — never as a side effect.
- [ ] Trade password unlocked MANUALLY in the OpenD GUI (never via SDK).
- [ ] `RISK_MARKET_HOLIDAYS` extended past 2026 (shipped default covers 2026 only).
- [ ] Human live-session exit gate (pre-existing, from Phase 1/2).

## Non-blocking follow-ups (tracked, deferred by the 2026-07-02 spec §10)

- Early-close (half-day) calendar support — required before holiday-season live.
- SimBroker total_assets ignores position market value (Minor #3).
- Overlay legs record limit_price for MARKET orders (Minor #4).
- Gated-overlay signal-row spam (Minor #5).
- Escalation intermediate orders absent from the trades projection (Minor #6).
- Planner stock anchor is always MARKET — document or change (Minor #7).
- UNKNOWN-ack reconciliation job (Minor #9).
- Machine-local dates in db._today()/runner._compute_realized (Minor #1).
- db._conn reach-ins in EODReporter._gather / runner (Minor #2).
```

Check off W-boxes that are already merged by the time this task runs.

- [ ] **Step 2: Fix CLAUDE.md env-var naming (review Minor #10b)**

In `CLAUDE.md`, replace exactly two occurrences:
- `Live trading requires **both** an explicit `TRADING_ENV=LIVE` environment flag` → `Live trading requires **both** an explicit `RISK_TRADING_ENV=LIVE` environment flag`
- `Default to paper trading. Live requires explicit `TRADING_ENV=LIVE`.` → `Default to paper trading. Live requires explicit `RISK_TRADING_ENV=LIVE`.`

(The config loader reads `RISK_TRADING_ENV` — `config.py:80`; the docs said `TRADING_ENV`, which would silently do nothing.)

- [ ] **Step 3: Verify and commit**

Run: `python -m pytest -q` (expect green; docs-only change)

```bash
git add PRE-LIVE.md CLAUDE.md
git commit -m "docs: PRE-LIVE gate checklist; fix TRADING_ENV -> RISK_TRADING_ENV

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

## Final verification (after Task 16)

- [ ] `python -m pytest -q` — full suite green, 0 regressions from the 519-test baseline (expect ~+45 new tests).
- [ ] `git status --short` — only intended files changed; the signals WIP files (`autotrader/signals/coerce.py`, `normalize.py` + their tests) remain untouched and uncommitted.
- [ ] Check off the completed W-boxes in `PRE-LIVE.md` and commit that update.
