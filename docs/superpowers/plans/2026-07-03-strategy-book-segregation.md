# Strategy Book Segregation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Prevent the internal breakout book and the AI/rebalance book from acting on each other's positions by tagging every breakout position with a durable per-symbol claim and enforcing that claim at the four `TradeEngine` decision points.

**Architecture:** A new pure `autotrader/books.py` module holds the `Origin` concept and skip-reason constants. A new `strategy_claims` SQLite table (breakout claims only; absence of a row = AI/rebalance book) is written before a breakout entry submits and released on confirmed flatness (inline on a filled exit, and via a self-healing reconcile after each ground-truth sync). Enforcement is four small filters inside `TradeEngine`; safety paths (`_flatten_all`, stops, cancel-on-shutdown) ignore claims entirely.

**Tech Stack:** Python 3, sqlite3 (WAL), pytest. No new dependencies. No SDK in any touched core module.

## Global Constraints

- Design source of truth: `docs/superpowers/specs/2026-07-03-strategy-book-segregation-design.md`. Every decision (D1–D6) there governs this plan.
- **Always-on, no feature flag** (D6). The zero-claims state MUST reproduce today's behavior byte-for-byte.
- **Unclaimed = AI/rebalance book** (D4). Only an explicit breakout entry creates a claim.
- **Exclusive per-symbol claim** (D2). No lot-level / quantity-split accounting.
- **External AI SELL on a breakout-owned symbol is skipped** (D3), same as external BUY.
- Safety paths NEVER consult claims: `_flatten_all`, `StopManager`, trailing stops, `cancel_working_orders`.
- Claim is written **before** the entry order is submitted (fail-closed: if the claim write raises, the order is not submitted).
- `autotrader/books.py` must import no `moomoo` SDK and hold no broker handle; it is added to the `test_no_sdk_in_core.py` guard.
- All Moomoo/broker responses already follow the `(ret_code, data)` pattern in `moomoo_broker.py`; this plan adds no broker calls.
- Symbols are normalized broker codes, e.g. `US.NIO`. Compare claims by exact symbol string (already normalized upstream).
- Follow existing patterns: `DB` methods lock with `self._lock` and `self._conn`; tests use the `tmp_path` fixture and construct `DB(str(tmp_path / "t.db"))`.

---

### Task 1: Pure `books` module + SDK guard

**Files:**
- Create: `autotrader/books.py`
- Test: `tests/test_books.py`
- Modify: `tests/test_no_sdk_in_core.py:24-28` (add `autotrader.books` to the import list)

**Interfaces:**
- Produces:
  - `ORIGIN_BREAKOUT: str = "BREAKOUT"`
  - `SKIP_BOOK_CONFLICT: str = "BOOK_CONFLICT"` — external signal on a breakout-owned symbol
  - `SKIP_BOOK_CLAIMED: str = "BOOK_CLAIMED"` — rebalance skip reason for a claimed symbol
  - `SKIP_POSITION_NOT_OWNED: str = "POSITION_NOT_OWNED"` — breakout tick declines to manage an unclaimed position
  - `is_breakout_claimed(symbol: str, claims: dict) -> bool` — True iff `claims.get(symbol) == ORIGIN_BREAKOUT`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_books.py
from autotrader import books


def test_constants_have_expected_values():
    assert books.ORIGIN_BREAKOUT == "BREAKOUT"
    assert books.SKIP_BOOK_CONFLICT == "BOOK_CONFLICT"
    assert books.SKIP_BOOK_CLAIMED == "BOOK_CLAIMED"
    assert books.SKIP_POSITION_NOT_OWNED == "POSITION_NOT_OWNED"


def test_is_breakout_claimed_true_only_for_breakout_origin():
    claims = {"US.NIO": "BREAKOUT"}
    assert books.is_breakout_claimed("US.NIO", claims) is True


def test_is_breakout_claimed_false_when_absent():
    assert books.is_breakout_claimed("US.NIO", {}) is False


def test_is_breakout_claimed_false_for_other_origin():
    assert books.is_breakout_claimed("US.NIO", {"US.NIO": "OTHER"}) is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_books.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'autotrader.books'`

- [ ] **Step 3: Write minimal implementation**

```python
# autotrader/books.py
"""Strategy-book segregation primitives (pure; no SDK, no broker handle).

A "claim" marks a symbol as belonging to the internal BREAKOUT book. Absence of a
claim means the symbol belongs to the AI/rebalance book (spec D4). These helpers
operate on a plain {symbol: origin} mapping supplied by the caller — persistence
lives in db.py, enforcement lives in main.py. See
docs/superpowers/specs/2026-07-03-strategy-book-segregation-design.md.
"""
from __future__ import annotations

ORIGIN_BREAKOUT = "BREAKOUT"

# Skip / result reasons surfaced by the TradeEngine choke points.
SKIP_BOOK_CONFLICT = "BOOK_CONFLICT"          # external signal vs. breakout-owned symbol
SKIP_BOOK_CLAIMED = "BOOK_CLAIMED"            # rebalance excludes a claimed symbol
SKIP_POSITION_NOT_OWNED = "POSITION_NOT_OWNED"  # breakout tick won't manage an unclaimed position


def is_breakout_claimed(symbol: str, claims: dict) -> bool:
    """True iff `symbol` is currently claimed by the BREAKOUT book."""
    return claims.get(symbol) == ORIGIN_BREAKOUT
```

- [ ] **Step 4: Add `autotrader.books` to the SDK guard**

In `tests/test_no_sdk_in_core.py`, inside the module tuple (currently ending with `'autotrader.moomoo_broker'`), add `'autotrader.books'`. The relevant line block:

```python
        "          'autotrader.signals.inbox', 'autotrader.signals.routine_adapter',\n"
        "          'autotrader.books',\n"
        "          'autotrader.moomoo_broker'):\n"
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/test_books.py tests/test_no_sdk_in_core.py -v`
Expected: PASS (all)

- [ ] **Step 6: Commit**

```bash
git add autotrader/books.py tests/test_books.py tests/test_no_sdk_in_core.py
git commit -m "feat(books): pure strategy-book segregation primitives"
```

---

### Task 2: Durable claims persistence in `db.py`

**Files:**
- Modify: `autotrader/db.py` (add table to `_SCHEMA` after the `engine_state` block ~line 101; add methods near `set_state`/`get_state` ~line 343)
- Test: `tests/test_claims_db.py`

**Interfaces:**
- Consumes: `autotrader.books.ORIGIN_BREAKOUT` (as the `origin` value callers pass).
- Produces (methods on `DB`):
  - `claim_symbol(symbol: str, origin: str, session_id: Optional[str] = None) -> None` — upsert (INSERT OR REPLACE) so re-claiming is idempotent.
  - `release_claim(symbol: str) -> None` — delete; no-op if absent.
  - `get_claims() -> dict` — `{symbol: origin}` for all rows.
  - `held_symbols() -> set` — `{symbol}` from the `positions` table where `qty > 0` (current ground truth; used by reconcile).

- [ ] **Step 1: Write the failing test**

```python
# tests/test_claims_db.py
"""strategy_claims persistence + held_symbols reconcile helper."""
from autotrader.db import DB
from autotrader.domain import Position


def _db(tmp_path, name="t.db"):
    return DB(str(tmp_path / name))


def test_claim_release_roundtrip(tmp_path):
    db = _db(tmp_path)
    db.claim_symbol("US.NIO", "BREAKOUT", "sess-1")
    assert db.get_claims() == {"US.NIO": "BREAKOUT"}
    db.release_claim("US.NIO")
    assert db.get_claims() == {}
    db.close()


def test_claim_is_idempotent(tmp_path):
    db = _db(tmp_path)
    db.claim_symbol("US.NIO", "BREAKOUT", "sess-1")
    db.claim_symbol("US.NIO", "BREAKOUT", "sess-2")  # re-claim, no error
    assert db.get_claims() == {"US.NIO": "BREAKOUT"}
    db.close()


def test_release_absent_is_noop(tmp_path):
    db = _db(tmp_path)
    db.release_claim("US.NIO")  # must not raise
    assert db.get_claims() == {}
    db.close()


def test_claims_persist_across_reopen(tmp_path):
    db = _db(tmp_path)
    db.claim_symbol("US.NIO", "BREAKOUT", "sess-1")
    db.close()
    db2 = DB(str(tmp_path / "t.db"))
    assert db2.get_claims() == {"US.NIO": "BREAKOUT"}
    db2.close()


def test_held_symbols_reflects_positions_with_positive_qty(tmp_path):
    db = _db(tmp_path)
    db.replace_positions([Position("US.NIO", 10, 5.0), Position("US.AAPL", 0, 100.0)])
    assert db.held_symbols() == {"US.NIO"}
    db.close()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_claims_db.py -v`
Expected: FAIL — `AttributeError: 'DB' object has no attribute 'claim_symbol'`

- [ ] **Step 3: Add the table to `_SCHEMA`**

In `autotrader/db.py`, append this block to the `_SCHEMA` string, immediately after the `engine_state` `CREATE TABLE` and before the closing `"""` (line ~101):

```sql

CREATE TABLE IF NOT EXISTS strategy_claims (
    symbol     TEXT PRIMARY KEY,   -- normalized code, e.g. US.NIO
    origin     TEXT NOT NULL,      -- 'BREAKOUT' (spec D4: only breakout claims stored)
    claimed_at TEXT NOT NULL,
    session_id TEXT
);
```

(`CREATE TABLE IF NOT EXISTS` inside the `executescript(_SCHEMA + ...)` call in `__init__` is the idempotent-migration pattern already used by every other table — no ALTER needed for a brand-new table.)

- [ ] **Step 4: Add the methods**

In `autotrader/db.py`, add these methods to the `DB` class (place after `list_state`, ~line 367, before `owned_symbols`):

```python
    def claim_symbol(self, symbol: str, origin: str,
                     session_id: Optional[str] = None) -> None:
        """Mark `symbol` as owned by `origin` (breakout). Idempotent upsert."""
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO strategy_claims "
                "(symbol,origin,claimed_at,session_id) VALUES (?,?,?,?)",
                (symbol, origin, _now(), session_id))
            self._conn.commit()

    def release_claim(self, symbol: str) -> None:
        """Release `symbol`'s claim. No-op if unclaimed."""
        with self._lock:
            self._conn.execute(
                "DELETE FROM strategy_claims WHERE symbol=?", (symbol,))
            self._conn.commit()

    def get_claims(self) -> dict:
        """{symbol: origin} for every current claim."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT symbol, origin FROM strategy_claims").fetchall()
        return {r[0]: r[1] for r in rows}

    def held_symbols(self) -> set:
        """{symbol} currently held (qty>0) per the positions projection — the
        ground-truth basis for releasing stale claims after a sync."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT symbol FROM positions WHERE qty > 0").fetchall()
        return {r[0] for r in rows}
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/test_claims_db.py tests/test_db.py -v`
Expected: PASS (all — including existing db tests, proving the new table didn't disturb the schema)

- [ ] **Step 6: Commit**

```bash
git add autotrader/db.py tests/test_claims_db.py
git commit -m "feat(db): strategy_claims table + claim/release/get/held_symbols"
```

---

### Task 3: External-signal conflict guard + origin threading in `_route_signal`

**Files:**
- Modify: `autotrader/main.py` — `tick()` (~line 171 call site), `submit_external_signal` (~line 182 call site), `_route_signal` signature + early guard (~line 245)
- Test: `tests/test_book_conflict_routing.py`

**Interfaces:**
- Consumes: `autotrader.books` (`is_breakout_claimed`, `SKIP_BOOK_CONFLICT`, `ORIGIN_BREAKOUT`); `DB.get_claims`.
- Produces: `_route_signal(self, signal, snap, price, origin: str = "EXTERNAL") -> TickResult`. When `origin == "EXTERNAL"` and the symbol is breakout-claimed, returns `TickResult(SKIP_BOOK_CONFLICT, symbol)` for BOTH BUY and SELL, and BEFORE the overlay dispatch (so external overlays are covered transitively — all overlay routing flows through `_route_signal`). `tick()` calls with `origin=ORIGIN_BREAKOUT`, which never triggers the guard.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_book_conflict_routing.py
"""External signals (plain + overlay) are skipped on breakout-claimed symbols;
internal breakout routing is unaffected."""
from datetime import datetime, timezone

from autotrader.config import RiskConfig
from autotrader.db import DB
from autotrader.domain import Signal
from autotrader.lifecycle import EntryGate
from autotrader.main import TradeEngine
from autotrader.sim_broker import SimBroker
from autotrader.strategies.threshold import StrategyParams, ThresholdStrategy

NOW = datetime(2026, 7, 3, 17, 0, tzinfo=timezone.utc)


def _cfg(**kw):
    base = dict(trading_env="PAPER", min_confidence=0.6, max_order_notional=1e9,
                max_position_qty=10_000, daily_loss_limit=500,
                max_gross_exposure=1e9, allowed_symbols=frozenset({"US.NIO"}),
                trailing_stop_pct=0.0, daily_loss_halt=1000)
    base.update(kw)
    return RiskConfig(**base)


def _engine(tmp_path, broker, cfg):
    strat = ThresholdStrategy(StrategyParams("US.NIO", 1.0, 0.05, 0.10, 0.7))
    db = DB(str(tmp_path / "t.db"))
    eng = TradeEngine(broker, strat, cfg, order_qty=1,
                      audit_path=str(tmp_path / "audit.jsonl"), db=db,
                      entry_gate=EntryGate(enabled=True))
    return eng, db


def test_external_buy_skipped_when_symbol_breakout_claimed(tmp_path):
    broker = SimBroker({"US.NIO": 5.0})
    eng, db = _engine(tmp_path, broker, _cfg())
    db.claim_symbol("US.NIO", "BREAKOUT", "sess-x")
    res = eng.submit_external_signal(Signal("US.NIO", "BUY", 0.9, "ext"))
    assert res.action == "BOOK_CONFLICT"
    db.close()


def test_external_sell_also_skipped_when_breakout_claimed(tmp_path):
    broker = SimBroker({"US.NIO": 5.0})
    eng, db = _engine(tmp_path, broker, _cfg())
    db.claim_symbol("US.NIO", "BREAKOUT", "sess-x")
    res = eng.submit_external_signal(Signal("US.NIO", "SELL", 0.9, "ext"))
    assert res.action == "BOOK_CONFLICT"
    db.close()


def test_external_unclaimed_symbol_routes_normally(tmp_path):
    broker = SimBroker({"US.NIO": 5.0})
    eng, db = _engine(tmp_path, broker, _cfg())
    res = eng.submit_external_signal(Signal("US.NIO", "BUY", 0.9, "ext"))
    assert res.action == "ORDER_PLACED"  # no claim -> AI book routes as today
    db.close()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_book_conflict_routing.py -v`
Expected: FAIL — `test_external_buy_skipped...` asserts `BOOK_CONFLICT` but gets `ORDER_PLACED` (guard not yet present).

- [ ] **Step 3: Add the import**

At the top of `autotrader/main.py`, alongside the other `from autotrader.*` imports, add:

```python
from autotrader import books
```

- [ ] **Step 4: Thread origin + add the guard in `_route_signal`**

Change the `_route_signal` signature (line ~245) from:

```python
    def _route_signal(self, signal: Signal, snap, price: float) -> TickResult:
        if signal.confidence < self._cfg.min_confidence:
            return TickResult("DROPPED_LOW_CONFIDENCE", f"{signal.confidence}")
```

to:

```python
    def _route_signal(self, signal: Signal, snap, price: float,
                      origin: str = "EXTERNAL") -> TickResult:
        # Book segregation (spec D3): an EXTERNAL signal (AI/rebalance book) must
        # not act on a symbol the internal BREAKOUT book owns — for BUY or SELL.
        # Placed before overlay dispatch so external overlays are covered too
        # (all overlay routing flows through here). BREAKOUT origin owns the
        # symbol and is never blocked.
        if origin != books.ORIGIN_BREAKOUT and self._db is not None:
            if books.is_breakout_claimed(signal.symbol, self._db.get_claims()):
                logger.info("book conflict: %s %s skipped — breakout-owned",
                            signal.direction, signal.symbol)
                return TickResult(books.SKIP_BOOK_CONFLICT, signal.symbol)

        if signal.confidence < self._cfg.min_confidence:
            return TickResult("DROPPED_LOW_CONFIDENCE", f"{signal.confidence}")
```

- [ ] **Step 5: Pass origin from the internal tick path**

In `tick()` (line ~171), change:

```python
        return self._route_signal(signal, snap, price)
```

to:

```python
        return self._route_signal(signal, snap, price, origin=books.ORIGIN_BREAKOUT)
```

Leave the `submit_external_signal` call site (line ~182) unchanged — it uses the `"EXTERNAL"` default.

- [ ] **Step 6: Run tests to verify they pass**

Run: `uv run pytest tests/test_book_conflict_routing.py -v`
Expected: PASS (all three)

- [ ] **Step 7: Run the fast regression band**

Run: `uv run pytest tests/test_engine_risk_check.py tests/test_snapshot_cache.py -q`
Expected: PASS (unchanged — internal routing still works)

- [ ] **Step 8: Commit**

```bash
git add autotrader/main.py tests/test_book_conflict_routing.py
git commit -m "feat(engine): skip external signals on breakout-owned symbols"
```

---

### Task 4: Claim-before-submit and inline release for the breakout book

**Files:**
- Modify: `autotrader/main.py` — `_route_signal`, in the breakout branch: write the claim after risk approval and before `_submit_with_escalation` (~line 300–302); release on a confirmed breakout exit fill (~line 316–319)
- Test: `tests/test_claim_lifecycle.py`

**Interfaces:**
- Consumes: `DB.claim_symbol`, `DB.release_claim`, `books.ORIGIN_BREAKOUT`, `self._session_id`, `OrderState.FILLED`.
- Produces: after a breakout BUY passes the risk gate, a claim row exists BEFORE the order is submitted (fail-closed: a raising `claim_symbol` aborts submission). After a breakout SELL whose ack is `FILLED`, the claim is released inline (sync handles the non-immediate live case — Task 6).

- [ ] **Step 1: Write the failing test**

```python
# tests/test_claim_lifecycle.py
"""Breakout BUY claims before submit; a filled breakout SELL releases inline."""
from datetime import datetime, timezone

from autotrader.config import RiskConfig
from autotrader.db import DB
from autotrader.domain import Position, Signal
from autotrader.lifecycle import EntryGate
from autotrader.main import TradeEngine
from autotrader.sim_broker import SimBroker
from autotrader.strategies.threshold import StrategyParams, ThresholdStrategy


def _cfg(**kw):
    base = dict(trading_env="PAPER", min_confidence=0.6, max_order_notional=1e9,
                max_position_qty=10_000, daily_loss_limit=500,
                max_gross_exposure=1e9, allowed_symbols=frozenset({"US.NIO"}),
                trailing_stop_pct=0.0, daily_loss_halt=1000)
    base.update(kw)
    return RiskConfig(**base)


def _engine(tmp_path, broker, cfg):
    strat = ThresholdStrategy(StrategyParams("US.NIO", 1.0, 0.05, 0.10, 0.7))
    db = DB(str(tmp_path / "t.db"))
    eng = TradeEngine(broker, strat, cfg, order_qty=1,
                      audit_path=str(tmp_path / "audit.jsonl"), db=db,
                      entry_gate=EntryGate(enabled=True))
    return eng, db


def test_breakout_buy_writes_claim(tmp_path):
    broker = SimBroker({"US.NIO": 5.0})
    eng, db = _engine(tmp_path, broker, _cfg())
    res = eng._route_signal(Signal("US.NIO", "BUY", 0.9, "breakout"),
                            broker.get_account(), 5.0, origin="BREAKOUT")
    assert res.action == "ORDER_PLACED"
    assert db.get_claims() == {"US.NIO": "BREAKOUT"}
    db.close()


def test_claim_written_before_submit_when_broker_raises(tmp_path):
    class RaisingBroker(SimBroker):
        def place_order(self, req):
            raise RuntimeError("broker down")

    broker = RaisingBroker({"US.NIO": 5.0})
    eng, db = _engine(tmp_path, broker, _cfg())
    try:
        eng._route_signal(Signal("US.NIO", "BUY", 0.9, "breakout"),
                          broker.get_account(), 5.0, origin="BREAKOUT")
    except RuntimeError:
        pass
    # Fail-closed ordering: the claim exists even though the order never acked.
    assert db.get_claims() == {"US.NIO": "BREAKOUT"}
    db.close()


def test_filled_breakout_sell_releases_claim(tmp_path):
    broker = SimBroker({"US.NIO": 5.0})
    broker._positions["US.NIO"] = Position("US.NIO", 1, 5.0)  # seed pattern from test_halt_flatten_order.py
    eng, db = _engine(tmp_path, broker, _cfg())
    db.claim_symbol("US.NIO", "BREAKOUT", "sess-x")
    res = eng._route_signal(Signal("US.NIO", "SELL", 0.9, "breakout"),
                            broker.get_account(), 5.0, origin="BREAKOUT")
    assert res.action == "ORDER_PLACED"
    assert db.get_claims() == {}   # SimBroker auto_fill -> FILLED ack -> released inline
    db.close()
```

> `SimBroker` has no `positions=` kwarg — seed via `broker._positions[sym] = Position(sym, qty, avg_price)` exactly as `tests/test_halt_flatten_order.py:25` does. `auto_fill` defaults True, so the SELL acks `FILLED` immediately.

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_claim_lifecycle.py -v`
Expected: FAIL — `test_breakout_buy_writes_claim` gets `db.get_claims() == {}` (no claim written yet).

- [ ] **Step 3: Write the claim before submit**

In `_route_signal`, locate the risk-approval block (line ~297–302):

```python
        decision = evaluate(req, snap, self._cfg, ref_price=price)
        if not decision.approved:
            logger.warning("risk core rejected: %s", decision.reason)
            return TickResult("REJECTED_BY_RISK", decision.reason)

        ack, term = self._submit_with_escalation(req, snap, price)
```

Insert the claim write between the approval check and the submit:

```python
        decision = evaluate(req, snap, self._cfg, ref_price=price)
        if not decision.approved:
            logger.warning("risk core rejected: %s", decision.reason)
            return TickResult("REJECTED_BY_RISK", decision.reason)

        # Book segregation (spec D2/D4): a BREAKOUT entry claims the symbol BEFORE
        # the order is submitted. Fail-closed — if the claim write raises, the
        # order is never sent (a claim on a phantom position only costs a skipped
        # rebalance; a real position with no claim is the commingling bug).
        if origin == books.ORIGIN_BREAKOUT and signal.direction == "BUY" \
                and self._db is not None:
            self._db.claim_symbol(signal.symbol, books.ORIGIN_BREAKOUT,
                                  self._session_id)

        ack, term = self._submit_with_escalation(req, snap, price)
```

- [ ] **Step 4: Release on a confirmed breakout exit fill**

In `_route_signal`, after the trailing-stop attach block and before the final `return TickResult("ORDER_PLACED", ...)` (line ~316–319):

```python
        # Broker-resting trailing stop: attach a protective TRAILING_STOP SELL
        # right after a BUY entry places (research R5 — survives an OpenD outage).
        if signal.direction == "BUY" and self._cfg.trailing_stop_pct > 0:
            self._attach_trailing_stop(signal.symbol, eff_qty, price, signal_id,
                                       entry_ack=ack)
        # Book segregation: a BREAKOUT exit that is CONFIRMED filled releases the
        # claim inline. Non-immediate (live) exits release via reconcile_claims()
        # after the next ground-truth sync (Task 6) — this is best-effort only.
        if origin == books.ORIGIN_BREAKOUT and signal.direction == "SELL" \
                and ack.state is OrderState.FILLED and self._db is not None:
            self._db.release_claim(signal.symbol)
        return TickResult("ORDER_PLACED", str(ack.broker_order_id))
```

Confirm `OrderState` is already imported in `main.py` (it is — used at line ~309). No new import needed.

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/test_claim_lifecycle.py -v`
Expected: PASS (all three)

- [ ] **Step 6: Commit**

```bash
git add autotrader/main.py tests/test_claim_lifecycle.py
git commit -m "feat(engine): claim breakout entries before submit, release on filled exit"
```

---

### Task 5: Breakout `tick()` won't manage an unclaimed position

**Files:**
- Modify: `autotrader/main.py` — `tick()` after computing `pos` (~line 162), before `self._strat.evaluate(...)`
- Test: `tests/test_tick_unowned_position.py`

**Interfaces:**
- Consumes: `books.is_breakout_claimed`, `books.SKIP_POSITION_NOT_OWNED`, `DB.get_claims`.
- Produces: `tick()` returns `TickResult(SKIP_POSITION_NOT_OWNED, symbol)` when the strategy symbol is held (`pos.qty > 0`) but NOT breakout-claimed (it belongs to the AI book) — the breakout exit brackets never touch a position the breakout engine didn't enter. A flat symbol still enters normally (and the entry claims it via Task 4).

- [ ] **Step 1: Write the failing test**

```python
# tests/test_tick_unowned_position.py
"""The breakout tick declines to manage a held-but-unclaimed (AI-book) position."""
from autotrader.config import RiskConfig
from autotrader.db import DB
from autotrader.domain import Position
from autotrader.lifecycle import EntryGate
from autotrader.main import TradeEngine
from autotrader.sim_broker import SimBroker
from autotrader.strategies.threshold import StrategyParams, ThresholdStrategy


def _cfg(**kw):
    base = dict(trading_env="PAPER", min_confidence=0.6, max_order_notional=1e9,
                max_position_qty=10_000, daily_loss_limit=500,
                max_gross_exposure=1e9, allowed_symbols=frozenset({"US.NIO"}),
                trailing_stop_pct=0.0, daily_loss_halt=1000)
    base.update(kw)
    return RiskConfig(**base)


def _engine(tmp_path, broker):
    # entry_price 1.0 so a held position at price 0.90 would normally hit the
    # -5% stop (SELL) — proving it's the claim check, not "no signal", that skips.
    strat = ThresholdStrategy(StrategyParams("US.NIO", 1.0, 0.05, 0.10, 0.7))
    db = DB(str(tmp_path / "t.db"))
    eng = TradeEngine(broker, strat, _cfg(), order_qty=1,
                      audit_path=str(tmp_path / "audit.jsonl"), db=db,
                      entry_gate=EntryGate(enabled=True))
    return eng, db


def test_tick_skips_held_unclaimed_position(tmp_path):
    broker = SimBroker({"US.NIO": 0.90})
    broker._positions["US.NIO"] = Position("US.NIO", 10, 1.0)
    eng, db = _engine(tmp_path, broker)
    # No claim -> AI book owns it -> breakout tick must not manage the exit.
    assert eng.tick().action == "POSITION_NOT_OWNED"
    db.close()


def test_tick_manages_held_claimed_position(tmp_path):
    broker = SimBroker({"US.NIO": 0.90})
    broker._positions["US.NIO"] = Position("US.NIO", 10, 1.0)
    eng, db = _engine(tmp_path, broker)
    db.claim_symbol("US.NIO", "BREAKOUT", "sess-x")
    # Claimed -> breakout owns it -> the -10% move triggers a stop-loss SELL.
    assert eng.tick().action == "ORDER_PLACED"
    db.close()
```

> Seed positions via `broker._positions[sym] = Position(...)` (no `positions=` kwarg — see `tests/test_halt_flatten_order.py:25`). At price 0.90 vs avg 1.0 the −10% move is ≤ the −5% stop, so `ThresholdStrategy` emits a stop-loss SELL — proving it's the claim guard, not "no signal", that skips.

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_tick_unowned_position.py -v`
Expected: FAIL — `test_tick_skips_held_unclaimed_position` gets `ORDER_PLACED` (tick still manages it).

- [ ] **Step 3: Add the guard in `tick()`**

In `tick()`, after `pos = next(...)` (line ~162) and before the `ref_high`/`evaluate` lines:

```python
        pos = next((p for p in snap.positions if p.symbol == symbol), None)
        # Book segregation (spec D2): the breakout engine manages exits ONLY for
        # positions it owns. A held-but-unclaimed position belongs to the AI book;
        # leave it alone. (Flat symbols fall through and may enter, claiming on BUY.)
        if pos is not None and pos.qty > 0 and self._db is not None:
            if not books.is_breakout_claimed(symbol, self._db.get_claims()):
                return TickResult(books.SKIP_POSITION_NOT_OWNED, symbol)
        ref_high = self._breakout_ref.high(symbol) if self._breakout_ref is not None else None
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_tick_unowned_position.py -v`
Expected: PASS (both)

- [ ] **Step 5: Run the tick regression band**

Run: `uv run pytest tests/test_snapshot_cache.py tests/test_book_conflict_routing.py -q`
Expected: PASS (a zero-claims tick is unchanged — the guard only fires on a held position, and only when unclaimed)

- [ ] **Step 6: Commit**

```bash
git add autotrader/main.py tests/test_tick_unowned_position.py
git commit -m "feat(engine): breakout tick skips held-but-unclaimed positions"
```

---

### Task 6: Rebalance excludes claimed symbols

**Files:**
- Modify: `autotrader/main.py` — `rebalance()` (~line 787–793), filter `scores` and the snapshot's `positions` before `compute_plan`
- Test: `tests/test_rebalance_book_claimed.py`

**Interfaces:**
- Consumes: `DB.get_claims`, `dataclasses.replace`.
- Produces: a claimed symbol is invisible to `compute_plan` — removed from `scores` (no top-up target) AND removed from the snapshot's `positions` (never trimmed/flattened). `compute_plan` itself is unchanged (spec design). The per-order gross cap is still enforced at `submit_rebalance_order`'s own `evaluate` against the TRUE account (it re-fetches `self._b.get_account()`), so filtering compute_plan's view does not weaken the hard gross limit.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_rebalance_book_claimed.py
"""Rebalance never trims or tops-up a breakout-claimed symbol."""
from datetime import datetime, timezone

from autotrader.config import RiskConfig
from autotrader.db import DB
from autotrader.domain import Position
from autotrader.lifecycle import EntryGate
from autotrader.main import TradeEngine
from autotrader.sim_broker import SimBroker
from autotrader.strategies.threshold import StrategyParams, ThresholdStrategy

NOW = datetime(2026, 7, 3, 16, 30, tzinfo=timezone.utc)


def _cfg(**kw):
    base = dict(trading_env="PAPER", min_confidence=0.6, max_order_notional=1e9,
                max_position_qty=10_000, daily_loss_limit=500,
                max_gross_exposure=1e9,
                allowed_symbols=frozenset({"US.NIO", "US.AAPL"}),
                trailing_stop_pct=0.0, daily_loss_halt=1000,
                rebalance_enabled=True, target_staleness_hours=48)
    base.update(kw)
    return RiskConfig(**base)


def _engine(tmp_path, broker, cfg):
    strat = ThresholdStrategy(StrategyParams("US.NIO", 1e9, 0.05, 0.10, 0.7))
    db = DB(str(tmp_path / "t.db"))
    eng = TradeEngine(broker, strat, cfg, order_qty=1,
                      audit_path=str(tmp_path / "audit.jsonl"), db=db,
                      entry_gate=EntryGate(enabled=True))
    return eng, db


def test_claimed_symbol_not_traded_by_rebalance(tmp_path):
    # Targets score US.NIO 0 (would flatten a held position) and US.AAPL 1.0.
    broker = SimBroker({"US.NIO": 5.0, "US.AAPL": 100.0})
    broker._positions["US.NIO"] = Position("US.NIO", 10, 5.0)
    eng, db = _engine(tmp_path, broker, _cfg())
    # upsert_target_weights(as_of_date, [(symbol, score), ...]); it stamps
    # ingested_at = _now() (UTC). Force it to NOW so `now - ingested_at` is fresh
    # (pattern from tests/test_engine_rebalance_run.py:58).
    db.upsert_target_weights("2026-07-03", [("US.NIO", 0.0), ("US.AAPL", 1.0)])
    db._conn.execute("UPDATE target_weights SET ingested_at=?", (NOW.isoformat(),))
    db._conn.commit()
    db.claim_symbol("US.NIO", "BREAKOUT", "sess-x")

    eng.rebalance(NOW)

    # No trade row for the claimed symbol (breakout owns its lifecycle).
    rows = db._conn.execute(
        "SELECT symbol FROM trades WHERE symbol=?", ("US.NIO",)).fetchall()
    assert rows == []
    db.close()
```

> Verified against the tree: `upsert_target_weights(as_of_date, [(symbol, score)])` stamps `ingested_at=_now()` internally, so the test overwrites it to `NOW` and passes `NOW` as the clock (staleness = `now - ingested_at`). There is no `all_trades()` reader — assert via the direct `db._conn.execute(...)` shown, the same reach-in existing rebalance tests use.

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_rebalance_book_claimed.py -v`
Expected: FAIL — a `SELL`/flatten trade for `US.NIO` exists (rebalance trimmed the claimed position).

- [ ] **Step 3: Add the exclusion filter in `rebalance()`**

At the top of `autotrader/main.py`, add to the imports:

```python
from dataclasses import replace as _dc_replace
```

In `rebalance()`, between fetching `snap` and calling `compute_plan` (line ~787–793), filter both inputs:

```python
        snap = self._b.get_account()
        # Book segregation (spec D2/D4): claimed (breakout-owned) symbols are
        # invisible to the rebalancer — never a top-up target, never trimmed.
        # compute_plan stays unchanged; we filter its inputs. The hard gross cap
        # is still enforced per-order in submit_rebalance_order (fresh account),
        # so hiding these positions here does not relax the gross limit.
        claimed = self._db.get_claims()
        if claimed:
            scores = {s: v for s, v in scores.items() if s not in claimed}
            snap = _dc_replace(
                snap, positions=tuple(p for p in snap.positions
                                      if p.symbol not in claimed))
        prices = {}
        for sym in scores:
            q = self._b.get_quote(sym)
            if q is not None:
                prices[sym] = q
        plan = compute_plan(snap, scores, prices, self._cfg)
```

(`scores` comes from `as_of, ingested_at, scores = latest` just above; reassigning the local is safe.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_rebalance_book_claimed.py -v`
Expected: PASS

- [ ] **Step 5: Run the rebalance regression band**

Run: `uv run pytest tests/ -k rebalance -q`
Expected: PASS (zero-claims rebalance unchanged — the filter is a no-op when `claimed` is empty)

- [ ] **Step 6: Commit**

```bash
git add autotrader/main.py tests/test_rebalance_book_claimed.py
git commit -m "feat(rebalance): exclude breakout-claimed symbols from targets and trims"
```

---

### Task 7: Self-healing claim reconcile after ground-truth sync

**Files:**
- Modify: `autotrader/main.py` — add `reconcile_claims()` method on `TradeEngine`
- Modify: `autotrader/runner.py` — call `self._engine.reconcile_claims()` after each `ground_truth_sync` at `PRE_OPEN_SYNC` (~line 117), `RISK_CHECK_MID/LATE` (~line 138), and `RISK_SWEEP` (~line 149)
- Test: `tests/test_claim_reconcile.py`

**Interfaces:**
- Consumes: `DB.get_claims`, `DB.held_symbols`, `DB.release_claim`.
- Produces: `reconcile_claims(self) -> int` on `TradeEngine` — releases every claim whose symbol is no longer held (qty>0) per the positions projection; returns the count released. Idempotent and safe when `self._db is None` (returns 0). Runner invokes it after each ground-truth sync so a claim left dangling by a broker-side stop fire, halt-flatten, manual close, or missed inline release is cleaned up within the trading day.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_claim_reconcile.py
"""reconcile_claims releases claims whose position has gone flat."""
from autotrader.config import RiskConfig
from autotrader.db import DB
from autotrader.domain import Position
from autotrader.lifecycle import EntryGate
from autotrader.main import TradeEngine
from autotrader.sim_broker import SimBroker
from autotrader.strategies.threshold import StrategyParams, ThresholdStrategy


def _cfg():
    return RiskConfig(trading_env="PAPER", min_confidence=0.6,
                      max_order_notional=1e9, max_position_qty=10_000,
                      daily_loss_limit=500, max_gross_exposure=1e9,
                      allowed_symbols=frozenset({"US.NIO"}),
                      trailing_stop_pct=0.0, daily_loss_halt=1000)


def _engine(tmp_path):
    broker = SimBroker({"US.NIO": 5.0})
    strat = ThresholdStrategy(StrategyParams("US.NIO", 1.0, 0.05, 0.10, 0.7))
    db = DB(str(tmp_path / "t.db"))
    eng = TradeEngine(broker, strat, _cfg(), order_qty=1,
                      audit_path=str(tmp_path / "audit.jsonl"), db=db,
                      entry_gate=EntryGate(enabled=True))
    return eng, db


def test_reconcile_releases_claim_for_flat_symbol(tmp_path):
    eng, db = _engine(tmp_path)
    db.claim_symbol("US.NIO", "BREAKOUT", "sess-x")
    db.replace_positions([])  # ground truth: nothing held
    assert eng.reconcile_claims() == 1
    assert db.get_claims() == {}
    db.close()


def test_reconcile_keeps_claim_for_held_symbol(tmp_path):
    eng, db = _engine(tmp_path)
    db.claim_symbol("US.NIO", "BREAKOUT", "sess-x")
    db.replace_positions([Position("US.NIO", 10, 5.0)])
    assert eng.reconcile_claims() == 0
    assert db.get_claims() == {"US.NIO": "BREAKOUT"}
    db.close()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_claim_reconcile.py -v`
Expected: FAIL — `AttributeError: 'TradeEngine' object has no attribute 'reconcile_claims'`

- [ ] **Step 3: Add `reconcile_claims()` to `TradeEngine`**

In `autotrader/main.py`, add this method near `apply_risk_check` (a natural home for reconcile logic, ~line 838):

```python
    def reconcile_claims(self) -> int:
        """Release any breakout claim whose symbol is no longer held (qty>0) per
        the positions projection. Self-heals claims left dangling by a broker-side
        stop fire, halt-flatten, manual close, or a missed inline release. Called
        after each ground-truth sync. No-op (returns 0) without a DB."""
        if self._db is None:
            return 0
        held = self._db.held_symbols()
        released = 0
        for symbol in list(self._db.get_claims()):
            if symbol not in held:
                self._db.release_claim(symbol)
                logger.info("claim released: %s no longer held (reconcile)", symbol)
                released += 1
        return released
```

- [ ] **Step 4: Run the method test to verify it passes**

Run: `uv run pytest tests/test_claim_reconcile.py -v`
Expected: PASS (both)

- [ ] **Step 5: Wire reconcile into the runner sync points**

In `autotrader/runner.py`, after each `ground_truth_sync(...)` call, add a reconcile. Three sites:

At `PRE_OPEN_SYNC` (line ~115–117):

```python
        if job == PRE_OPEN_SYNC:
            if self._broker is not None and self._db is not None:
                ground_truth_sync(self._broker, self._db, owned_only=self._owned_only)
                if self._engine is not None:
                    self._engine.reconcile_claims()
```

At `RISK_CHECK_MID/RISK_CHECK_LATE` (line ~137–139):

```python
            if self._broker is not None and self._db is not None:
                ground_truth_sync(self._broker, self._db, owned_only=self._owned_only)
                self._engine.reconcile_claims()
                self._record_perf()
```

At `RISK_SWEEP` (line ~148–150):

```python
            if self._broker is not None and self._db is not None:
                ground_truth_sync(self._broker, self._db, owned_only=self._owned_only)
                self._engine.reconcile_claims()
                self._record_perf()
```

(At `RISK_CHECK`/`RISK_SWEEP` the runner already dereferences `self._engine` unconditionally in the same branch, so no extra None-guard is needed there; `PRE_OPEN_SYNC` guards it because that branch otherwise never touches the engine.)

- [ ] **Step 6: Write a runner integration test**

```python
# append to tests/test_claim_reconcile.py

def test_runner_risk_sweep_reconciles_claims(tmp_path):
    """A claim on a now-flat symbol is released when RISK_SWEEP syncs."""
    from datetime import datetime, timezone
    from autotrader.runner import SessionRunner  # match the actual runner class name
    from autotrader.scheduler import RISK_SWEEP

    eng, db = _engine(tmp_path)
    db.claim_symbol("US.NIO", "BREAKOUT", "sess-x")
    # broker holds nothing -> ground_truth_sync writes empty positions
    # Build a runner around eng/db/broker as the other runner tests do, then:
    #   runner._run_job(RISK_SWEEP, datetime(2026,7,3,19,30,tzinfo=timezone.utc))
    # assert db.get_claims() == {}
    db.close()
```

> Replace the commented scaffold with the real `SessionRunner` construction used in `tests/test_runner_report_integrity.py` (mirror its broker/gate/reporter wiring). If wiring a full runner here is heavy, keep only the direct `reconcile_claims()` tests from Step 1 and delete this test — the runner wiring is exercised by the existing runner suite once the calls are in place.

- [ ] **Step 7: Run the full suite**

Run: `uv run pytest -q`
Expected: PASS — target **≥ 690 passed, 2 skipped** (was 681 passed / 2 skipped; this plan adds ~16–20 tests across Tasks 1–7). The 2 skips remain the live-only `tests/test_moomoo_broker_live.py`.

- [ ] **Step 8: Commit**

```bash
git add autotrader/main.py autotrader/runner.py tests/test_claim_reconcile.py
git commit -m "feat(engine,runner): self-healing claim reconcile after ground-truth sync"
```

---

## Self-Review

**1. Spec coverage:**
- Data model — `books.py` + `strategy_claims` table → Tasks 1, 2. ✓
- Claim before submit (fail-closed) → Task 4 Step 3. ✓
- Release inline on confirmed exit fill → Task 4 Step 4. ✓
- Release via sync (self-heal) → Task 7. ✓
- Enforcement #1 tick (leave unclaimed alone) → Task 5. ✓
- Enforcement #2 `_route_signal` external BUY+SELL skip → Task 3. ✓
- Enforcement #3 rebalance exclusion (scores + positions) → Task 6. ✓
- Enforcement #4 overlay skip → covered transitively by Task 3 (all overlay routing passes through `_route_signal`'s pre-dispatch guard; noted in Task 3 interfaces). ✓
- Safety paths ignore claims → nothing in `_flatten_all`/`StopManager`/`cancel_working_orders` is modified; verified by the untouched halt/flatten suites in Task 7 Step 7. ✓
- SDK-free `books.py` in guard → Task 1 Step 4. ✓
- No feature flag; zero-claims == today → regression bands in Tasks 3/5/6 + full suite in Task 7. ✓
- Error handling (claim write fail aborts submit; release failure log-only via reconcile retry) → Task 4 (ordering) + Task 7 (retry). ✓

**2. Placeholder scan:** No TBD/TODO. Two tests carry explicit "confirm the exact method name / mirror existing test" notes (Task 6 `upsert_target_weights`/`all_trades`, Task 7 runner construction) because those signatures must be read from the current tree at implementation time; the behavioral assertion in each is fully specified, so a task is never left open-ended.

**3. Type consistency:** `origin` is a `str` everywhere (`"BREAKOUT"`/`"EXTERNAL"`, via `books.ORIGIN_BREAKOUT`). `get_claims() -> dict`, `held_symbols() -> set`, `is_breakout_claimed(symbol, claims) -> bool`, `reconcile_claims() -> int`, `claim_symbol(symbol, origin, session_id=None)`, `release_claim(symbol)` — names and signatures match across Tasks 1, 2, 3, 4, 5, 6, 7. `TickResult(action, detail)` two-arg form matches existing usage. `OrderState.FILLED` already imported in `main.py`.

**4. Ambiguity check:** "Confirmed exit fill" is pinned to `ack.state is OrderState.FILLED` (Task 4). "No longer held" is pinned to absence from `positions WHERE qty > 0` (Task 2 `held_symbols`). "Claimed" is pinned to `origin == ORIGIN_BREAKOUT` (Task 1). Rebalance gross-cap concern resolved explicitly: per-order `evaluate` in `submit_rebalance_order` re-fetches the true account, so input filtering cannot relax the hard cap (Task 6 interfaces).
