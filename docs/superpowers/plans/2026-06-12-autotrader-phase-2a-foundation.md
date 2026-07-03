# AutoTrader Phase 2a — Foundation (Carry-over Fixes · Rate Limiter · SQLite WAL)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix four carry-over defects from the Phase 1 review, add a token-bucket rate limiter guarding Moomoo API limits, and add a SQLite WAL projection recording every signal, trade, and performance snapshot — all without breaking the existing 54-test offline suite.

**Architecture:** `autotrader/rate_limiter.py` is a pure-Python token bucket injected into `MoomooBroker` as two instance-level buckets (15 order tokens / 30 s, 10 refresh tokens / 30 s). `autotrader/db.py` wraps sqlite3 WAL mode with a `DB` class that is injected into `TradeEngine` as an optional parameter — existing tests pass `None` and remain unchanged. No new runtime dependencies (sqlite3 is stdlib; pydantic belongs in Plan 2c).

**Tech Stack:** Python 3.11 · sqlite3 (stdlib) · threading (stdlib) · pytest ≥ 8.0

---

## Phase 2 Scope Note

Phase 2 from the roadmap covers seven items across three independent subsystems. This plan covers the **foundation layer only**. The two follow-on plans are:
- **Plan 2b — Engines:** subscribe-driven continuous loop + EST lifecycle scheduler + watchdog
- **Plan 2c — Signals:** Pydantic external-signal ingress + broker-resting trailing stops

Each plan delivers working, independently testable software and should be executed in order (2a → 2b → 2c).

---

## Acceptance Criteria

All of the following must be green before this plan is complete:

| AC | Command / Check |
|---|---|
| AC-1 SELL sizing | `pytest tests/test_main_loop.py::test_tick_sell_uses_position_qty_not_order_qty` → PASS |
| AC-2 reconcile since | `pytest tests/test_moomoo_broker_offline.py::test_reconcile_fills_since_passes_begin_time` → PASS |
| AC-3 reconcile no-since | `pytest tests/test_moomoo_broker_offline.py::test_reconcile_fills_no_since_omits_begin_time` → PASS |
| AC-4 rate limiter | `pytest tests/test_rate_limiter.py` → 4 passed |
| AC-5 rate limit in broker | `pytest tests/test_moomoo_broker_offline.py::test_place_order_raises_rate_limit_when_order_limiter_drained` → PASS |
| AC-6 SQLite WAL | `pytest tests/test_db.py` → 7 passed |
| AC-7 DB wiring | `pytest tests/test_main_loop.py::test_tick_records_signal_and_trade_to_db` → PASS |
| AC-8 full suite | `pytest tests/ --ignore=tests/test_moomoo_broker_live.py` → 70 passed, 0 skipped |

---

## File Structure

| File | Action | Responsibility |
|---|---|---|
| `autotrader/rate_limiter.py` | **Create** | Token bucket: `capacity`, `refill_rate`, `acquire(timeout)→bool` |
| `autotrader/db.py` | **Create** | SQLite WAL; 6 tables; `DB.record_signal/record_trade/record_fills/upsert_positions/record_performance/record_halt/resolve_halt` |
| `autotrader/main.py` | **Modify** | Fix SELL sizing in `tick()`; add optional `db` param to `TradeEngine` |
| `autotrader/moomoo_broker.py` | **Modify** | `reconcile_fills` `since` filter; cancel logging; inject two `RateLimiter` instances |
| `tests/test_rate_limiter.py` | **Create** | 4 tests: acquire, timeout, tokens-refill, thread-safety |
| `tests/test_db.py` | **Create** | 7 tests: one per write method + parent-dir creation |
| `tests/test_main_loop.py` | **Modify** | Add SELL-sizing test; add DB-wiring test |
| `tests/test_moomoo_broker_offline.py` | **Modify** | Update `_broker()` helper; add reconcile-since tests + rate-limit test |
| `config/risk.config.example` | **Modify** | Document `AUTOTRADER_DB_PATH` |

---

### Task 1: Fix SELL sizing — use position.qty not order_qty

**Files:**
- Modify: `autotrader/main.py` (lines 53–57 in `tick()`)
- Modify: `tests/test_main_loop.py`

- [ ] **Step 1: Write the failing test**

Add at the bottom of `tests/test_main_loop.py`:

```python
from autotrader.domain import OrderRequest


def test_tick_sell_uses_position_qty_not_order_qty(tmp_path):
    """A SELL exit must liquidate the full position, not just order_qty shares."""
    b = SimBroker(quotes={"US.AAPL": 94.0}, cash=100000.0)
    # Create a position of 10 shares at avg_price=120 via a direct limit order.
    # stop_loss_pct=0.05 -> stop at 120*0.95=114; quote 94 < 114 -> stop triggers.
    b.place_order(OrderRequest(symbol="US.AAPL", side="BUY", qty=10,
                               order_type="LIMIT", limit_price=120.0,
                               client_order_id="setup-cid"))
    strat = ThresholdStrategy(StrategyParams(symbol="US.AAPL", entry_price=999.0,
                                             stop_loss_pct=0.05, take_profit_pct=0.10,
                                             confidence=0.7))
    # order_qty=1, position=10 — the SELL should be for 10, not 1.
    eng = TradeEngine(broker=b, strategy=strat, cfg=_cfg(), order_qty=1,
                      audit_path=str(tmp_path / "audit.jsonl"))
    result = eng.tick()
    assert result.action == "ORDER_PLACED"
    all_fills = b.reconcile_fills(None)
    sell_fills = [f for f in all_fills if f.side == "SELL"]
    assert len(sell_fills) == 1, "exactly one SELL fill expected"
    assert sell_fills[0].qty == 10, f"expected qty 10 (position qty), got {sell_fills[0].qty}"
```

- [ ] **Step 2: Run test to verify it fails**

```bash
cd /Users/acdc/Documents/AI/AutoTrader
python3 -m pytest tests/test_main_loop.py::test_tick_sell_uses_position_qty_not_order_qty -v
```

Expected: FAIL — `AssertionError: expected qty 10 (position qty), got 1`

- [ ] **Step 3: Fix SELL sizing in TradeEngine.tick()**

In `autotrader/main.py`, replace the block starting at `self._signal_seq += 1`:

```python
        self._signal_seq += 1
        cid = OrderRouter.make_client_order_id(
            signal.symbol, signal.direction, self._qty, f"sig-{self._signal_seq}")
        req = OrderRequest(symbol=signal.symbol, side=signal.direction, qty=self._qty,
                           order_type="MARKET", limit_price=None, client_order_id=cid)
```

with:

```python
        self._signal_seq += 1
        # SELL exits must liquidate the full position; BUY uses the configured order_qty.
        sell_qty = pos.qty if (signal.direction == "SELL" and pos is not None) else self._qty
        cid = OrderRouter.make_client_order_id(
            signal.symbol, signal.direction, sell_qty, f"sig-{self._signal_seq}")
        req = OrderRequest(symbol=signal.symbol, side=signal.direction, qty=sell_qty,
                           order_type="MARKET", limit_price=None, client_order_id=cid)
```

- [ ] **Step 4: Run all main-loop tests**

```bash
python3 -m pytest tests/test_main_loop.py -v
```

Expected: 7 passed (6 original + 1 new).

- [ ] **Step 5: Commit**

```bash
git add autotrader/main.py tests/test_main_loop.py
git commit -m "fix: SELL exits use position.qty not order_qty"
```

---

### Task 2: reconcile_fills since filter + per-order cancel logging

**Files:**
- Modify: `autotrader/moomoo_broker.py` (reconcile_fills + cancel_order + cancel_all)
- Modify: `tests/test_moomoo_broker_offline.py`

- [ ] **Step 1: Write the failing tests**

Add after the last test in `tests/test_moomoo_broker_offline.py`:

```python
class _CapturingTrade:
    """Records kwargs passed to deal_list_query for inspection."""
    def __init__(self):
        self.captured = {}

    def deal_list_query(self, **kwargs):
        self.captured.update(kwargs)
        return 0, None  # RET_OK=0, empty data -> reconcile returns []


def _broker_with_trade(trade):
    """Extend the existing _broker() helper with a specific trade context."""
    b = MoomooBroker.__new__(MoomooBroker)
    b._c = _FakeCommon()
    b._trade = trade
    b._quote = None
    b._acc_id = 1
    # Rate limiters don't exist yet (added in Task 4). Tests here call reconcile_fills
    # which will get the limiter from __init__ once wired; for now the method is called
    # directly, so no limiter needed on this object.
    return b


def test_reconcile_fills_since_passes_begin_time():
    """reconcile_fills(since=...) must forward begin_time to deal_list_query."""
    trade = _CapturingTrade()
    b = _broker_with_trade(trade)
    b.reconcile_fills(since="2026-06-12 09:30:00")
    assert "begin_time" in trade.captured, "begin_time must be forwarded when since is set"
    assert trade.captured["begin_time"] == "2026-06-12 09:30:00"


def test_reconcile_fills_no_since_omits_begin_time():
    """reconcile_fills(since=None) must NOT pass begin_time to deal_list_query."""
    trade = _CapturingTrade()
    b = _broker_with_trade(trade)
    b.reconcile_fills(since=None)
    assert "begin_time" not in trade.captured, "begin_time must be absent when since=None"
```

- [ ] **Step 2: Run the tests to verify they fail**

```bash
python3 -m pytest tests/test_moomoo_broker_offline.py::test_reconcile_fills_since_passes_begin_time \
                  tests/test_moomoo_broker_offline.py::test_reconcile_fills_no_since_omits_begin_time -v
```

Expected: FAIL — `begin_time` is never passed in the current implementation.

- [ ] **Step 3: Fix reconcile_fills to forward since as begin_time**

In `autotrader/moomoo_broker.py`, add `import logging` at the top (just below the existing imports) and replace `reconcile_fills`:

```python
logger = logging.getLogger("autotrader.broker")
```

Then replace the `reconcile_fills` method:

```python
    def reconcile_fills(self, since: Optional[str]) -> List[Fill]:
        kwargs = dict(trd_env=self._env(), acc_id=self._acc_id, refresh_cache=True)
        if since:
            kwargs["begin_time"] = since
        ret, data = self._trade.deal_list_query(**kwargs)
        if not self._ok(ret) or self._c.is_empty(data):
            return []
        out: List[Fill] = []
        for i in range(len(data)):
            row = data.iloc[i]
            side_raw = self._c.format_enum(self._c.safe_get(row, "trd_side", default="BUY")).upper()
            out.append(Fill(
                fill_id=str(self._c.safe_get(row, "deal_id", default="")),
                symbol=str(self._c.safe_get(row, "code", default="")),
                side="BUY" if side_raw == "BUY" else "SELL",
                qty=self._c.safe_float(self._c.safe_get(row, "qty", default=0)),
                price=self._c.safe_float(self._c.safe_get(row, "price", default=0)),
                ts=str(self._c.safe_get(row, "create_time", default="")),
            ))
        return out
```

- [ ] **Step 4: Add per-order cancel logging**

In `autotrader/moomoo_broker.py`, replace `cancel_order` and `cancel_all`:

```python
    def cancel_order(self, broker_order_id: str) -> None:
        from moomoo import ModifyOrderOp  # confined to this adapter
        logger.info("cancel_order: %s", broker_order_id)
        ret, data = self._trade.modify_order(
            modify_order_op=ModifyOrderOp.CANCEL, order_id=broker_order_id,
            qty=0, price=0, trd_env=self._env(), acc_id=self._acc_id)
        if not self._ok(ret):
            raise BrokerError(BrokerErrorKind.UNKNOWN, f"cancel failed: {data}")

    def cancel_all(self) -> None:
        orders = self.get_open_orders()
        logger.info("cancel_all: %d open order(s) to cancel", len(orders))
        for ack in orders:
            if ack.broker_order_id:
                try:
                    self.cancel_order(ack.broker_order_id)
                except BrokerError:
                    pass  # best-effort flatten on shutdown; logged by cancel_order
```

- [ ] **Step 5: Run the full offline suite**

```bash
python3 -m pytest tests/test_moomoo_broker_offline.py tests/test_main_loop.py -v
```

Expected: 5 existing broker tests + 2 new reconcile tests + 7 main-loop tests = 14 passed.

- [ ] **Step 6: Commit**

```bash
git add autotrader/moomoo_broker.py tests/test_moomoo_broker_offline.py
git commit -m "fix: reconcile_fills forwards since as begin_time; add cancel logging"
```

---

### Task 3: Rate limiter token bucket

**Files:**
- Create: `autotrader/rate_limiter.py`
- Create: `tests/test_rate_limiter.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_rate_limiter.py`:

```python
"""Token bucket rate limiter tests. No mocks needed — the class is pure Python."""
import time

import pytest


def test_acquire_consumes_token_and_returns_true():
    from autotrader.rate_limiter import RateLimiter
    rl = RateLimiter(capacity=3.0, refill_rate=1.0)
    assert rl.acquire(timeout=0.1) is True


def test_acquire_returns_false_when_drained_and_timeout_expires():
    from autotrader.rate_limiter import RateLimiter
    # capacity=1, refill_rate=0 -> tokens never refill
    rl = RateLimiter(capacity=1.0, refill_rate=0.0)
    assert rl.acquire(timeout=0.5) is True   # consumes the one token
    assert rl.acquire(timeout=0.1) is False  # drained, timeout fires


def test_tokens_refill_over_time():
    from autotrader.rate_limiter import RateLimiter
    # Refill at 200 tokens/sec -> 1 token refills in 0.005s. Drain and wait 0.05s.
    rl = RateLimiter(capacity=1.0, refill_rate=200.0)
    assert rl.acquire(timeout=0.1) is True   # consume the 1 token
    time.sleep(0.05)                          # wait for refill (200/s -> ~0.005s needed)
    assert rl.acquire(timeout=0.1) is True   # should be available again


def test_acquire_is_thread_safe():
    """Concurrent acquires on a capacity-10 limiter must each get exactly one token."""
    import threading
    from autotrader.rate_limiter import RateLimiter

    rl = RateLimiter(capacity=10.0, refill_rate=0.0)  # no refill — 10 tokens total
    results = []
    lock = threading.Lock()

    def grab():
        got = rl.acquire(timeout=0.5)
        with lock:
            results.append(got)

    threads = [threading.Thread(target=grab) for _ in range(15)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # Exactly 10 acquires succeed; 5 time out.
    assert results.count(True) == 10
    assert results.count(False) == 5
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
python3 -m pytest tests/test_rate_limiter.py -v
```

Expected: FAIL with `ModuleNotFoundError: No module named 'autotrader.rate_limiter'`

- [ ] **Step 3: Create autotrader/rate_limiter.py**

Create `autotrader/rate_limiter.py`:

```python
"""Token bucket rate limiter.

Two instances live in MoomooBroker: one for order submissions
(capacity=15, refill_rate=15/30) and one for refresh-cache queries
(capacity=10, refill_rate=10/30), matching Moomoo API limits.
"""
from __future__ import annotations

import threading
import time


class RateLimiter:
    """Token bucket. acquire() blocks until a token is available or timeout expires.

    capacity:     burst ceiling (tokens)
    refill_rate:  tokens added per second
    """

    def __init__(self, capacity: float, refill_rate: float):
        self._capacity = capacity
        self._refill_rate = refill_rate
        self._tokens = float(capacity)
        self._last = time.monotonic()
        self._lock = threading.Lock()

    def acquire(self, timeout: float = 60.0) -> bool:
        """Consume one token, blocking until available. Returns False on timeout."""
        deadline = time.monotonic() + timeout
        while True:
            with self._lock:
                now = time.monotonic()
                self._tokens = min(
                    self._capacity,
                    self._tokens + (now - self._last) * self._refill_rate,
                )
                self._last = now
                if self._tokens >= 1.0:
                    self._tokens -= 1.0
                    return True
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.02)  # poll at 50 Hz; a full 30 s window has 15 tokens
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
python3 -m pytest tests/test_rate_limiter.py -v
```

Expected: 4 passed.

- [ ] **Step 5: Commit**

```bash
git add autotrader/rate_limiter.py tests/test_rate_limiter.py
git commit -m "feat: add token-bucket RateLimiter for Moomoo API limits"
```

---

### Task 4: Wire rate limiters into MoomooBroker

**Files:**
- Modify: `autotrader/moomoo_broker.py`
- Modify: `tests/test_moomoo_broker_offline.py`

- [ ] **Step 1: Write the failing test**

Add at the bottom of `tests/test_moomoo_broker_offline.py`:

```python
def test_place_order_raises_rate_limit_when_order_limiter_drained():
    """place_order must raise BrokerError(RATE_LIMIT) if the order bucket is empty."""
    from autotrader.domain import BrokerError, BrokerErrorKind, OrderRequest
    from autotrader.rate_limiter import RateLimiter

    class _AlwaysTimeout:
        def acquire(self, timeout=60.0):
            return False  # simulates an always-drained bucket

    b = MoomooBroker.__new__(MoomooBroker)
    b._order_rl = _AlwaysTimeout()
    b._refresh_rl = RateLimiter(capacity=10, refill_rate=10 / 30)

    req = OrderRequest(symbol="US.AAPL", side="BUY", qty=1,
                       order_type="MARKET", limit_price=None,
                       client_order_id="test-cid")
    with pytest.raises(BrokerError) as exc_info:
        b.place_order(req)
    assert exc_info.value.kind == BrokerErrorKind.RATE_LIMIT
```

- [ ] **Step 2: Run test to verify it fails**

```bash
python3 -m pytest tests/test_moomoo_broker_offline.py::test_place_order_raises_rate_limit_when_order_limiter_drained -v
```

Expected: FAIL with `AttributeError: 'MoomooBroker' object has no attribute '_order_rl'`

- [ ] **Step 3: Update the _broker() helper to inject rate limiters**

In `tests/test_moomoo_broker_offline.py`, replace `_broker()`:

```python
def _broker(trade):
    from autotrader.rate_limiter import RateLimiter
    b = MoomooBroker.__new__(MoomooBroker)  # bypass __init__ (no OpenD needed)
    b._c = _FakeCommon()
    b._trade = trade
    b._quote = None
    b._acc_id = 1
    b._order_rl = RateLimiter(capacity=15.0, refill_rate=0.5)
    b._refresh_rl = RateLimiter(capacity=10.0, refill_rate=10 / 30)
    return b
```

Also update `_broker_with_trade()` (added in Task 2) to inject rate limiters:

```python
def _broker_with_trade(trade):
    from autotrader.rate_limiter import RateLimiter
    b = MoomooBroker.__new__(MoomooBroker)
    b._c = _FakeCommon()
    b._trade = trade
    b._quote = None
    b._acc_id = 1
    b._order_rl = RateLimiter(capacity=15.0, refill_rate=0.5)
    b._refresh_rl = RateLimiter(capacity=10.0, refill_rate=10 / 30)
    return b
```

- [ ] **Step 4: Add rate limiters to MoomooBroker**

In `autotrader/moomoo_broker.py`, add the import at the top:

```python
from autotrader.rate_limiter import RateLimiter
```

In `MoomooBroker.__init__`, add after `self._quote = None`:

```python
        self._order_rl = RateLimiter(capacity=15.0, refill_rate=0.5)    # 15 orders / 30 s
        self._refresh_rl = RateLimiter(capacity=10.0, refill_rate=10 / 30)  # 10 refresh / 30 s
```

At the start of `place_order`, before the `if req.order_type` line, add:

```python
        if not self._order_rl.acquire(timeout=60.0):
            raise BrokerError(BrokerErrorKind.RATE_LIMIT,
                               "order rate limit: timed out waiting for order token")
```

At the start of `get_account`, before `ret, acc = self._trade.accinfo_query(...)`, add:

```python
        if not self._refresh_rl.acquire(timeout=60.0):
            raise BrokerError(BrokerErrorKind.RATE_LIMIT,
                               "refresh rate limit: timed out waiting for account token")
```

At the start of `_positions`, before `ret, data = self._trade.position_list_query(...)`, add:

```python
        if not self._refresh_rl.acquire(timeout=60.0):
            return None  # treat rate-limit failure as a failed query -> stale snapshot
```

At the start of `get_open_orders`, before `ret, data = self._trade.order_list_query(...)`, add:

```python
        if not self._refresh_rl.acquire(timeout=60.0):
            return []  # best-effort on shutdown/monitoring path
```

At the start of `reconcile_fills`, before `kwargs = dict(...)`, add:

```python
        if not self._refresh_rl.acquire(timeout=60.0):
            return []
```

- [ ] **Step 5: Run the full offline broker test suite**

```bash
python3 -m pytest tests/test_moomoo_broker_offline.py -v
```

Expected: 8 passed (5 original staleness tests + 2 reconcile-since tests + 1 rate-limit test).

- [ ] **Step 6: Run the full offline suite to check nothing regressed**

```bash
python3 -m pytest tests/ --ignore=tests/test_moomoo_broker_live.py -v
```

Expected: 62 passed (54 baseline + 1 SELL sizing + 2 reconcile + 4 rate-limiter + 1 rate-limit-broker = 62).

- [ ] **Step 7: Commit**

```bash
git add autotrader/moomoo_broker.py tests/test_moomoo_broker_offline.py
git commit -m "feat: wire Moomoo API rate limiters into MoomooBroker"
```

---

### Task 5: SQLite WAL projection

**Files:**
- Create: `autotrader/db.py`
- Create: `tests/test_db.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_db.py`:

```python
"""SQLite WAL projection tests. All use tmp_path to avoid stale state."""
import pytest


def _db(tmp_path, name="test.db"):
    from autotrader.db import DB
    return DB(str(tmp_path / name))


def test_record_signal_stores_and_dedupes_by_signal_id(tmp_path):
    db = _db(tmp_path)
    db.record_signal("US.AAPL", "BUY", 0.8, "above entry", "sig-1")
    db.record_signal("US.AAPL", "BUY", 0.9, "still above", "sig-1")  # duplicate
    count = db._conn.execute("SELECT COUNT(*) FROM signals").fetchone()[0]
    assert count == 1, "INSERT OR IGNORE must dedupe on signal_id"
    db.close()


def test_record_trade_stores_and_replaces_on_duplicate_cid(tmp_path):
    db = _db(tmp_path)
    db.record_trade("cid-1", "US.AAPL", "BUY", 10, "MARKET", None, "boid-1", "SUBMITTED")
    db.record_trade("cid-1", "US.AAPL", "BUY", 10, "MARKET", None, "boid-1", "FILLED")
    row = db._conn.execute("SELECT state FROM trades WHERE client_order_id='cid-1'").fetchone()
    assert row[0] == "FILLED", "INSERT OR REPLACE must update state on duplicate cid"
    db.close()


def test_record_fills_idempotent_on_duplicate_fill_id(tmp_path):
    from autotrader.domain import Fill
    db = _db(tmp_path)
    fills = [Fill("fid-1", "US.AAPL", "BUY", 5.0, 150.0, "2026-06-12T10:00:00+00:00")]
    inserted = db.record_fills(fills)
    assert inserted == 1
    inserted_again = db.record_fills(fills)  # duplicate
    assert inserted_again == 0, "INSERT OR IGNORE must dedupe on fill_id"
    db.close()


def test_upsert_positions_stores_and_overwrites(tmp_path):
    from autotrader.domain import Position
    db = _db(tmp_path)
    db.upsert_positions([Position("US.AAPL", 10, 150.0)])
    db.upsert_positions([Position("US.AAPL", 15, 152.0)])  # update
    row = db._conn.execute("SELECT qty, avg_price FROM positions WHERE symbol='US.AAPL'").fetchone()
    assert row == (15, 152.0)
    db.close()


def test_record_performance_upserts_by_date(tmp_path):
    db = _db(tmp_path)
    db.record_performance(day_pnl=100.0, total_assets=10500.0, cash=500.0, gross_exposure=10000.0)
    db.record_performance(day_pnl=200.0, total_assets=10600.0, cash=400.0, gross_exposure=10200.0)
    count = db._conn.execute("SELECT COUNT(*) FROM performance").fetchone()[0]
    assert count == 1, "performance upserts by date (one row per trading day)"
    row = db._conn.execute("SELECT day_pnl FROM performance").fetchone()
    assert row[0] == 200.0, "second upsert should overwrite day_pnl"
    db.close()


def test_record_halt_and_resolve(tmp_path):
    db = _db(tmp_path)
    halt_id = db.record_halt("daily loss limit breached")
    resolved = db._conn.execute(
        "SELECT resolved_at FROM halts WHERE id=?", (halt_id,)
    ).fetchone()[0]
    assert resolved is None, "halt should be unresolved on creation"
    db.resolve_halt(halt_id)
    resolved = db._conn.execute(
        "SELECT resolved_at FROM halts WHERE id=?", (halt_id,)
    ).fetchone()[0]
    assert resolved is not None, "resolve_halt must set resolved_at"
    db.close()


def test_db_creates_parent_directory_if_missing(tmp_path):
    from autotrader.db import DB
    nested = str(tmp_path / "nested" / "deep" / "autotrader.db")
    db = DB(nested)
    db.record_halt("test")
    count = db._conn.execute("SELECT COUNT(*) FROM halts").fetchone()[0]
    assert count == 1
    db.close()
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
python3 -m pytest tests/test_db.py -v
```

Expected: FAIL with `ModuleNotFoundError: No module named 'autotrader.db'`

- [ ] **Step 3: Create autotrader/db.py**

Create `autotrader/db.py`:

```python
"""SQLite WAL projection — an incrementally-populated read-through cache of
broker ground truth.

Tables:
  signals     — every Signal value that passed the confidence filter
  trades      — every OrderRequest + OrderAck (keyed by client_order_id)
  fills       — every Fill received from the broker (keyed by fill_id, idempotent)
  positions   — latest position snapshot per symbol (upserted by symbol)
  performance — one row per calendar date (upserted)
  halts       — soft-halt events with optional resolution timestamp

The JSONL audit journal is the immutable source of record for orders; this
projection is rebuilt from it on startup in Phase 3+.
"""
from __future__ import annotations

import sqlite3
import threading
from datetime import date, datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, List, Optional

if TYPE_CHECKING:
    from autotrader.domain import Fill, Position


_SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS signals (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    ts            TEXT NOT NULL,
    symbol        TEXT NOT NULL,
    direction     TEXT NOT NULL CHECK(direction IN ('BUY','SELL')),
    confidence    REAL NOT NULL,
    rationale     TEXT NOT NULL,
    signal_id     TEXT NOT NULL UNIQUE
);

CREATE TABLE IF NOT EXISTS trades (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    ts              TEXT NOT NULL,
    client_order_id TEXT NOT NULL UNIQUE,
    symbol          TEXT NOT NULL,
    side            TEXT NOT NULL CHECK(side IN ('BUY','SELL')),
    qty             INTEGER NOT NULL,
    order_type      TEXT NOT NULL,
    limit_price     REAL,
    broker_order_id TEXT,
    state           TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS fills (
    fill_id   TEXT PRIMARY KEY,
    ts        TEXT NOT NULL,
    symbol    TEXT NOT NULL,
    side      TEXT NOT NULL CHECK(side IN ('BUY','SELL')),
    qty       REAL NOT NULL,
    price     REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS positions (
    symbol     TEXT PRIMARY KEY,
    qty        INTEGER NOT NULL,
    avg_price  REAL NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS performance (
    date           TEXT PRIMARY KEY,
    day_pnl        REAL NOT NULL,
    total_assets   REAL NOT NULL,
    cash           REAL NOT NULL,
    gross_exposure REAL NOT NULL,
    updated_at     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS halts (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    ts           TEXT NOT NULL,
    reason       TEXT NOT NULL,
    resolved_at  TEXT
);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _today() -> str:
    return date.today().isoformat()


class DB:
    def __init__(self, path: str):
        db_dir = Path(path).parent
        db_dir.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.executescript(_SCHEMA)
        self._conn.commit()
        self._lock = threading.Lock()

    def record_signal(self, symbol: str, direction: str, confidence: float,
                      rationale: str, signal_id: str) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT OR IGNORE INTO signals "
                "(ts,symbol,direction,confidence,rationale,signal_id) VALUES (?,?,?,?,?,?)",
                (_now(), symbol, direction, confidence, rationale, signal_id),
            )
            self._conn.commit()

    def record_trade(self, client_order_id: str, symbol: str, side: str, qty: int,
                     order_type: str, limit_price: Optional[float],
                     broker_order_id: Optional[str], state: str) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO trades "
                "(ts,client_order_id,symbol,side,qty,order_type,limit_price,"
                "broker_order_id,state) VALUES (?,?,?,?,?,?,?,?,?)",
                (_now(), client_order_id, symbol, side, qty, order_type,
                 limit_price, broker_order_id, state),
            )
            self._conn.commit()

    def record_fills(self, fills: List) -> int:
        """Upsert fills by fill_id (idempotent). Returns count of newly inserted rows."""
        inserted = 0
        with self._lock:
            for f in fills:
                cur = self._conn.execute(
                    "INSERT OR IGNORE INTO fills (fill_id,ts,symbol,side,qty,price) "
                    "VALUES (?,?,?,?,?,?)",
                    (f.fill_id, f.ts, f.symbol, f.side, f.qty, f.price),
                )
                inserted += cur.rowcount
            self._conn.commit()
        return inserted

    def upsert_positions(self, positions: List) -> None:
        ts = _now()
        with self._lock:
            for p in positions:
                self._conn.execute(
                    "INSERT OR REPLACE INTO positions (symbol,qty,avg_price,updated_at) "
                    "VALUES (?,?,?,?)",
                    (p.symbol, p.qty, p.avg_price, ts),
                )
            self._conn.commit()

    def record_performance(self, day_pnl: float, total_assets: float,
                           cash: float, gross_exposure: float) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO performance "
                "(date,day_pnl,total_assets,cash,gross_exposure,updated_at) "
                "VALUES (?,?,?,?,?,?)",
                (_today(), day_pnl, total_assets, cash, gross_exposure, _now()),
            )
            self._conn.commit()

    def record_halt(self, reason: str) -> int:
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO halts (ts,reason) VALUES (?,?)", (_now(), reason)
            )
            self._conn.commit()
        return cur.lastrowid

    def resolve_halt(self, halt_id: int) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE halts SET resolved_at=? WHERE id=?", (_now(), halt_id)
            )
            self._conn.commit()

    def close(self) -> None:
        self._conn.close()
```

- [ ] **Step 4: Run the DB tests**

```bash
python3 -m pytest tests/test_db.py -v
```

Expected: 7 passed.

- [ ] **Step 5: Commit**

```bash
git add autotrader/db.py tests/test_db.py
git commit -m "feat: SQLite WAL projection (db.py) with 6-table schema"
```

---

### Task 6: Wire DB into TradeEngine and main()

**Files:**
- Modify: `autotrader/main.py`
- Modify: `config/risk.config.example`
- Modify: `tests/test_main_loop.py`

- [ ] **Step 1: Write the failing test**

Add at the bottom of `tests/test_main_loop.py`:

```python
def test_tick_records_signal_and_trade_to_db(tmp_path):
    """When a DB is injected, tick() writes the signal and trade to it."""
    from autotrader.db import DB
    db = DB(str(tmp_path / "autotrader.db"))
    b = SimBroker(quotes={"US.AAPL": 101.0}, cash=100000.0)
    strat = ThresholdStrategy(StrategyParams(symbol="US.AAPL", entry_price=100.0,
                                             stop_loss_pct=0.05, take_profit_pct=0.10,
                                             confidence=0.7))
    eng = TradeEngine(broker=b, strategy=strat, cfg=_cfg(), order_qty=10,
                      audit_path=str(tmp_path / "audit.jsonl"), db=db)
    result = eng.tick()
    assert result.action == "ORDER_PLACED"
    sig_count = db._conn.execute("SELECT COUNT(*) FROM signals").fetchone()[0]
    assert sig_count == 1, "signal must be recorded"
    trade_count = db._conn.execute("SELECT COUNT(*) FROM trades").fetchone()[0]
    assert trade_count == 1, "trade must be recorded"
    perf_count = db._conn.execute("SELECT COUNT(*) FROM performance").fetchone()[0]
    assert perf_count == 1, "performance snapshot must be recorded"
    db.close()
```

- [ ] **Step 2: Run test to verify it fails**

```bash
python3 -m pytest tests/test_main_loop.py::test_tick_records_signal_and_trade_to_db -v
```

Expected: FAIL with `TypeError: TradeEngine.__init__() got an unexpected keyword argument 'db'`

- [ ] **Step 3: Add optional db parameter to TradeEngine**

In `autotrader/main.py`, add the import at the top (after existing imports):

```python
from typing import TYPE_CHECKING, Optional
if TYPE_CHECKING:
    from autotrader.db import DB
```

Replace `TradeEngine.__init__` signature:

```python
    def __init__(self, broker: Broker, strategy: ThresholdStrategy, cfg: RiskConfig,
                 order_qty: int, audit_path: str, db: "Optional[DB]" = None):
        self._b = broker
        self._strat = strategy
        self._cfg = cfg
        self._qty = order_qty
        self._router = OrderRouter(broker, audit_path=audit_path)
        self._signal_seq = 0
        self._db = db
```

In `tick()`, after `if signal.confidence < self._cfg.min_confidence:` block (i.e., after the signal passes the confidence filter), add the signal recording call just before `self._signal_seq += 1`:

```python
        if self._db:
            self._db.record_performance(
                day_pnl=snap.day_pnl,
                total_assets=snap.total_assets,
                cash=snap.cash,
                gross_exposure=snap.gross_exposure(),
            )
```

Then after the `sell_qty` / `cid` / `req` block (i.e., after the signal passes the confidence filter and before the risk core call), add:

```python
        signal_id = f"sig-{self._signal_seq}"
        if self._db:
            self._db.record_signal(
                symbol=signal.symbol, direction=signal.direction,
                confidence=signal.confidence, rationale=signal.rationale,
                signal_id=signal_id,
            )
```

NOTE: you must update `cid = OrderRouter.make_client_order_id(...)` to use `signal_id` instead of the inline string:

```python
        cid = OrderRouter.make_client_order_id(signal.symbol, signal.direction, sell_qty, signal_id)
```

After the `ack = self._router.submit(req)` line and before the return, add:

```python
        if self._db:
            self._db.record_trade(
                client_order_id=ack.client_order_id,
                symbol=req.symbol,
                side=req.side,
                qty=req.qty,
                order_type=req.order_type,
                limit_price=req.limit_price,
                broker_order_id=ack.broker_order_id,
                state=ack.state.value,
            )
```

The full updated `tick()` method must look exactly like this (verify no steps are missing):

```python
    def tick(self) -> TickResult:
        snap = self._b.get_account()
        symbol = self._strat.p.symbol
        price = self._b.get_quote(symbol)
        if price is None:
            return TickResult("NO_QUOTE", symbol)

        pos = next((p for p in snap.positions if p.symbol == symbol), None)
        signal = self._strat.evaluate(price=price, position=pos)
        if signal is None:
            return TickResult("NO_SIGNAL")

        if signal.confidence < self._cfg.min_confidence:
            return TickResult("DROPPED_LOW_CONFIDENCE", f"{signal.confidence}")

        if self._db:
            self._db.record_performance(
                day_pnl=snap.day_pnl,
                total_assets=snap.total_assets,
                cash=snap.cash,
                gross_exposure=snap.gross_exposure(),
            )

        self._signal_seq += 1
        signal_id = f"sig-{self._signal_seq}"
        if self._db:
            self._db.record_signal(
                symbol=signal.symbol, direction=signal.direction,
                confidence=signal.confidence, rationale=signal.rationale,
                signal_id=signal_id,
            )

        sell_qty = pos.qty if (signal.direction == "SELL" and pos is not None) else self._qty
        cid = OrderRouter.make_client_order_id(signal.symbol, signal.direction, sell_qty, signal_id)
        req = OrderRequest(symbol=signal.symbol, side=signal.direction, qty=sell_qty,
                           order_type="MARKET", limit_price=None, client_order_id=cid)

        decision = evaluate(req, snap, self._cfg, ref_price=price)
        if not decision.approved:
            logger.warning("risk core rejected: %s", decision.reason)
            return TickResult("REJECTED_BY_RISK", decision.reason)

        ack = self._router.submit(req)
        if self._db:
            self._db.record_trade(
                client_order_id=ack.client_order_id,
                symbol=req.symbol,
                side=req.side,
                qty=req.qty,
                order_type=req.order_type,
                limit_price=req.limit_price,
                broker_order_id=ack.broker_order_id,
                state=ack.state.value,
            )
        if ack.state is OrderState.UNKNOWN:
            return TickResult("ORDER_UNKNOWN", ack.client_order_id)
        if ack.state is OrderState.REJECTED:
            return TickResult("ORDER_REJECTED", ack.client_order_id)
        return TickResult("ORDER_PLACED", str(ack.broker_order_id))
```

- [ ] **Step 4: Wire DB into main()**

In `autotrader/main.py`, replace the `main()` function's broker/engine setup block. Add the DB setup after `broker = MoomooBroker()`:

```python
    db_path = os.path.expanduser(os.getenv("AUTOTRADER_DB_PATH", "~/.autotrader.db"))
    from autotrader.db import DB  # lazy import: keeps tests that skip main() SDK-free
    db = DB(db_path)
    logger.info("DB projection at %s", db_path)
```

Pass `db=db` to `TradeEngine(...)`:

```python
    engine = TradeEngine(broker, strat, cfg, order_qty=int(os.getenv("ORDER_QTY", "1")),
                         audit_path=audit, db=db)
```

Add `db.close()` in the finally block after `broker.close()`:

```python
    finally:
        try:
            engine.shutdown()
        except Exception as e:
            logger.error("shutdown error (working orders may remain): %s", e)
        broker.close()
        db.close()
```

- [ ] **Step 5: Document AUTOTRADER_DB_PATH in config example**

In `config/risk.config.example`, add at the bottom:

```bash
# Path for the SQLite WAL projection. Default: ~/.autotrader.db
# AUTOTRADER_DB_PATH=~/.autotrader.db
```

- [ ] **Step 6: Run the full test suite**

```bash
python3 -m pytest tests/ --ignore=tests/test_moomoo_broker_live.py -v
```

Expected: 70 passed, 0 skipped.

- [ ] **Step 7: Commit**

```bash
git add autotrader/main.py config/risk.config.example tests/test_main_loop.py
git commit -m "feat: wire optional DB projection into TradeEngine; document AUTOTRADER_DB_PATH"
```

---

## Self-Review

### 1. Spec coverage

Checking each item from the Phase 2 roadmap carry-over list:

| Roadmap item | Covered by task |
|---|---|
| SELL exits sized to `position.qty` | Task 1 ✓ |
| `reconcile_fills(since=…)` server-side filter | Task 2 ✓ |
| Per-order cancel logging | Task 2 ✓ |
| Rate-limit token bucket (15 orders/30s, 10 refresh/30s) | Tasks 3+4 ✓ |
| SQLite WAL projection (`signals`, `trades`, `fills`, `positions`, `performance`, `halts`) | Task 5 ✓ |
| Dashboard reads from SQLite (Phase 2 goal) | NOT in scope for 2a — dashboard integration is a Phase 2b task tied to the scheduler's ground-truth sync |

### 2. Placeholder scan

No "TBD", "TODO", "implement later", or "similar to Task N" patterns present. Every step includes complete code or an exact command.

### 3. Type consistency

- `DB.record_fills(fills: List)` takes domain `Fill` objects — consistent with `broker.reconcile_fills() -> List[Fill]` ✓
- `DB.upsert_positions(positions: List)` takes domain `Position` objects — consistent with `AccountSnapshot.positions: Tuple[Position, ...]` ✓
- `TradeEngine(db=db)` — `db` is `Optional[DB]` — consistent with `None` default in tests ✓
- `signal_id = f"sig-{self._signal_seq}"` — used in both `make_client_order_id()` and `record_signal()` ✓
- `_broker_with_trade()` and `_broker()` both inject `_order_rl` and `_refresh_rl` ✓

---

Plan complete and saved to `docs/superpowers/plans/2026-06-12-autotrader-phase-2a-foundation.md`.

**Two execution options:**

**1. Subagent-Driven (recommended)** — I dispatch a fresh subagent per task with two-stage spec + quality review after each.

**2. Inline Execution** — Execute tasks in this session with checkpoints.

**Which approach?**
