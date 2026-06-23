# Rebalance Order-Slicing (Cap-Aware Top-Up Sizing) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make `compute_plan` clamp each rebalance top-up to the largest qty that passes all three risk caps, so orders place and converge across rounds instead of being rejected.

**Architecture:** Single-file change to `compute_plan` in `autotrader/rebalance.py`, TOPUP branch only. Seed a `running_gross` accumulator at the snapshot's current gross exposure and clamp each top-up against per-order notional, resulting-position-qty, and remaining gross headroom; increment `running_gross` per emitted top-up. Pure function; no `risk_core`/executor change.

**Tech Stack:** Python 3, pytest. Pure function over `AccountSnapshot` + `RiskConfig`.

## Global Constraints

- **TOPUP/BUY only.** The TRIM branch is unchanged — trims are reduce-only SELLs that `evaluate()` already exempts from these caps; the covered-floor cap stays as-is.
- **All three caps:** `qty = min(drift_qty, max_order_notional//price, max_position_qty − current_qty, gross_room//price)`.
- **Running gross:** seed `running_gross = snapshot.gross_exposure()` once before the symbol loop; increment by `qty * price` only when a top-up is appended. Deterministic `sorted(fractions)` order. Do NOT subtract the round's trims (conservative, by design).
- **New skip reason `CAPPED`** when the clamped top-up qty is `≤ 0` (no headroom). `WITHIN_BAND` and `SKIPPED_MIN_NOTIONAL` semantics unchanged; min-notional check runs AFTER the clamp.
- **No `risk_core`/executor/config change.** `evaluate()` remains the backstop.
- **RiskConfig fields (exact):** `max_order_notional: float`, `max_position_qty: int`, `max_gross_exposure: float`, `rebalance_min_notional: float`.
- **Out of scope:** raising any cap; multi-slice per symbol; trim sizing; modeling trim-freed gross.

---

### Task 1: Cap-aware top-up clamp in `compute_plan`

**Files:**
- Modify: `autotrader/rebalance.py` (TOPUP/underweight branch of `compute_plan`, ~lines 102-106; add `running_gross` init before the `for symbol in sorted(fractions)` loop, ~line 68)
- Test: `tests/test_rebalance.py`

**Interfaces:**
- Consumes: `AccountSnapshot.gross_exposure() -> float`, `AccountSnapshot.position_qty(symbol) -> int`, `RiskConfig.{max_order_notional, max_position_qty, max_gross_exposure, rebalance_min_notional}`.
- Produces: a new skip reason string `"CAPPED"` in `RebalancePlan.skipped`; TOPUP `qty` clamped so each placed order passes all three caps and `running_gross` never exceeds `max_gross_exposure`.

> All commands run from `/Users/acdc/Documents/AI/AutoTrader`. The test file already imports `AccountSnapshot`, `Position`, `compute_plan`, and defines `_cfg(**kw)` (defaults: `max_order_notional=1e9`, `max_position_qty=10_000`, `max_gross_exposure=1e9`, `allowed_symbols={US.AAPL, US.MSFT}`, `rebalance_band_pct=5.0`, `rebalance_min_notional=200.0`, `rebalance_cash_buffer_pct=10.0`).

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_rebalance.py`:

```python
def test_topup_clamped_to_notional_cap():
    # No AAPL held; single-symbol allow-list → target_weight 0.9 → target_value 9000.
    # Full drift wants 90 sh; notional cap 5000 / price 100 → clamp to 50.
    snap = AccountSnapshot(cash=10000.0, total_assets=10000.0, day_pnl=0.0,
                           stale=False, positions=())
    cfg = _cfg(allowed_symbols=frozenset({"US.AAPL"}), max_order_notional=5000.0)
    plan = compute_plan(snap, {"US.AAPL": 100.0}, {"US.AAPL": 100.0}, cfg)
    assert len(plan.trades) == 1
    t = plan.trades[0]
    assert (t.symbol, t.side, t.action) == ("US.AAPL", "BUY", "TOPUP")
    assert t.qty == 50 and t.new_total_qty == 50


def test_topup_clamped_to_position_qty_cap():
    # 90 sh held, cap 100 → only 10 more allowed even though drift wants more.
    snap = AccountSnapshot(cash=0.0, total_assets=20000.0, day_pnl=0.0,
                           stale=False, positions=(Position("US.AAPL", 90, 100.0),))
    cfg = _cfg(allowed_symbols=frozenset({"US.AAPL"}), max_position_qty=100)
    plan = compute_plan(snap, {"US.AAPL": 100.0}, {"US.AAPL": 100.0}, cfg)
    assert len(plan.trades) == 1
    t = plan.trades[0]
    assert t.qty == 10 and t.new_total_qty == 100


def test_topup_clamped_to_gross_headroom_in_sorted_order():
    # Two scored names, each target_value 9000 (0.45 * 20000), both want 90 sh.
    # gross cap 10000, start gross 0: AAPL (sorted first) takes 90 (=9000),
    # MSFT clamped to remaining headroom 1000 → 10 sh.
    snap = AccountSnapshot(cash=0.0, total_assets=20000.0, day_pnl=0.0,
                           stale=False, positions=())
    cfg = _cfg(max_gross_exposure=10000.0)  # allow-list defaults to {AAPL, MSFT}
    plan = compute_plan(snap, {"US.AAPL": 100.0, "US.MSFT": 100.0},
                        {"US.AAPL": 100.0, "US.MSFT": 100.0}, cfg)
    by_sym = {t.symbol: t for t in plan.trades}
    assert by_sym["US.AAPL"].qty == 90
    assert by_sym["US.MSFT"].qty == 10   # clamped by remaining gross headroom


def test_topup_capped_skip_when_at_qty_cap():
    # Already at the position cap → no headroom → CAPPED, no trade.
    snap = AccountSnapshot(cash=0.0, total_assets=40000.0, day_pnl=0.0,
                           stale=False, positions=(Position("US.AAPL", 100, 100.0),))
    cfg = _cfg(allowed_symbols=frozenset({"US.AAPL"}), max_position_qty=100)
    plan = compute_plan(snap, {"US.AAPL": 100.0}, {"US.AAPL": 100.0}, cfg)
    assert plan.trades == ()
    assert ("US.AAPL", "CAPPED") in plan.skipped


def test_topup_clamp_below_min_notional_skipped():
    # Notional cap 150 / price 100 → clamp to 1 sh = $100 < min_notional 200 → skip.
    snap = AccountSnapshot(cash=10000.0, total_assets=10000.0, day_pnl=0.0,
                           stale=False, positions=())
    cfg = _cfg(allowed_symbols=frozenset({"US.AAPL"}),
               max_order_notional=150.0, rebalance_min_notional=200.0)
    plan = compute_plan(snap, {"US.AAPL": 100.0}, {"US.AAPL": 100.0}, cfg)
    assert plan.trades == ()
    assert ("US.AAPL", "SKIPPED_MIN_NOTIONAL") in plan.skipped


def test_topup_within_caps_is_unchanged():
    # Regression: drift well within all (default-high) caps tops up the full drift.
    # 10 sh held, target_value 9000 → drift (9000-1000)//100 = 80, unclamped.
    snap = AccountSnapshot(cash=9000.0, total_assets=10000.0, day_pnl=0.0,
                           stale=False, positions=(Position("US.AAPL", 10, 100.0),))
    cfg = _cfg(allowed_symbols=frozenset({"US.AAPL"}))
    plan = compute_plan(snap, {"US.AAPL": 100.0}, {"US.AAPL": 100.0}, cfg)
    assert len(plan.trades) == 1
    assert plan.trades[0].qty == 80 and plan.trades[0].new_total_qty == 90


def test_trim_not_clamped_by_topup_caps():
    # TOPUP-only: an overweight name trims its full size even with a tiny notional
    # cap (trims are reduce-only and bypass these caps).
    snap = AccountSnapshot(cash=2000.0, total_assets=10000.0, day_pnl=0.0,
                           stale=False, positions=(Position("US.AAPL", 80, 100.0),))
    cfg = _cfg(max_order_notional=100.0)  # default 2-symbol allow-list → target 0.45
    plan = compute_plan(snap, {"US.AAPL": 100.0}, {"US.AAPL": 100.0}, cfg)
    assert len(plan.trades) == 1
    t = plan.trades[0]
    assert (t.symbol, t.side, t.action) == ("US.AAPL", "SELL", "TRIM")
    assert t.qty == 35 and t.new_total_qty == 45
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python3 -m pytest tests/test_rebalance.py -k "topup or trim_not_clamped" -v`
Expected: FAIL — the clamp/`CAPPED`/`running_gross` logic does not exist yet, so e.g. `test_topup_clamped_to_notional_cap` sees `qty == 90` (full drift) instead of 50, and `test_topup_capped_skip_when_at_qty_cap` finds a trade / no `CAPPED` skip. (`test_topup_within_caps_is_unchanged` and `test_trim_not_clamped_by_topup_caps` may already pass — that is fine; they are regression guards.)

- [ ] **Step 3: Add `running_gross` init + the clamp**

In `compute_plan`, add the accumulator just before the symbol loop. The current code (~lines 63-69) is:

```python
    trims: list = []
    topups: list = []
    skipped: list = []
    if total <= 0:
        return RebalancePlan((), tuple((s, "NO_EQUITY") for s in fractions))

    for symbol in sorted(fractions):
```

Change to add one line:

```python
    trims: list = []
    topups: list = []
    skipped: list = []
    if total <= 0:
        return RebalancePlan((), tuple((s, "NO_EQUITY") for s in fractions))

    running_gross = snapshot.gross_exposure()
    for symbol in sorted(fractions):
```

Then replace the TOPUP/underweight branch. The current code (~lines 102-106) is:

```python
        else:              # underweight -> top up
            qty = int((target_value - current_value) // price)
            if qty <= 0:
                skipped.append((symbol, "WITHIN_BAND"))
                continue
            if qty * price < cfg.rebalance_min_notional:
                skipped.append((symbol, "SKIPPED_MIN_NOTIONAL"))
                continue
            topups.append(RebalanceTrade(symbol, "BUY", qty, "TOPUP",
                                         current_qty + qty))
```

Replace it with:

```python
        else:              # underweight -> top up
            qty = int((target_value - current_value) // price)
            if qty <= 0:
                skipped.append((symbol, "WITHIN_BAND"))
                continue
            # Clamp to the largest qty that passes all three risk caps, so the
            # order places (and converges across rounds) instead of being
            # rejected. running_gross tracks projected gross across this plan's
            # top-ups (conservative: the round's trims, which free real gross,
            # are not subtracted).
            notional_cap_qty = int(cfg.max_order_notional // price)
            qty_cap_room = max(0, cfg.max_position_qty - current_qty)
            gross_room_qty = int(max(0.0, cfg.max_gross_exposure - running_gross) // price)
            qty = min(qty, notional_cap_qty, qty_cap_room, gross_room_qty)
            if qty <= 0:
                skipped.append((symbol, "CAPPED"))
                continue
            if qty * price < cfg.rebalance_min_notional:
                skipped.append((symbol, "SKIPPED_MIN_NOTIONAL"))
                continue
            topups.append(RebalanceTrade(symbol, "BUY", qty, "TOPUP",
                                         current_qty + qty))
            running_gross += qty * price
```

(The TRIM branch above it is unchanged.)

- [ ] **Step 4: Run the new tests to verify they pass**

Run: `python3 -m pytest tests/test_rebalance.py -k "topup or trim_not_clamped" -v`
Expected: PASS (7 tests).

- [ ] **Step 5: Run the full rebalance suite (no regressions)**

Run: `python3 -m pytest tests/test_rebalance.py -q`
Expected: PASS — pre-existing tests still pass. The `_cfg` defaults (`max_order_notional=1e9`, `max_position_qty=10_000`, `max_gross_exposure=1e9`) make the clamp a no-op for every existing top-up test, so their asserted qtys are unchanged.

- [ ] **Step 6: Commit**

```bash
git add autotrader/rebalance.py tests/test_rebalance.py
git commit -m "feat(rebalance): clamp top-ups to the risk-cap envelope

compute_plan sizes each TOPUP to min(drift, notional/price,
max_position_qty-current, gross_room/price), tracking running gross across
the plan, so orders place and converge across rounds instead of being
rejected by risk_core. New CAPPED skip when no headroom remains. TOPUP-only;
trims (reduce-only) and the covered-floor cap are untouched."
```

---

## Post-implementation note (not a task)

After this lands, the rebalancer places cap-respecting orders that converge toward
target up to the cap envelope. Fully deploying the ~95%-cash book to the
score-weighted target *values* still requires a separate, human-reviewed change to
`RISK_MAX_POSITION_QTY` / `RISK_MAX_ORDER_NOTIONAL` / `RISK_MAX_GROSS_EXPOSURE`
(spec §2 non-goal) — out of scope here.

## Self-Review

- **Spec coverage:** S1 all-three-caps → Step 3 clamp (`notional_cap_qty`/`qty_cap_room`/`gross_room_qty`), tests 1-3. S2 one-order-per-symbol-per-round → single clamped `topups.append`; convergence noted. S3 compute_plan-only → no other file touched. S4 TOPUP-only → TRIM branch unchanged, `test_trim_not_clamped_by_topup_caps`. S5 running_gross seed + conservative (no trim subtraction) → Step 3 init + comment, `test_topup_clamped_to_gross_headroom_in_sorted_order`. S6 `CAPPED` skip → `test_topup_capped_skip_when_at_qty_cap`. §6 edge cases: price>notional (notional_cap_qty=0→CAPPED), at qty cap (test 4), gross exhausted (test 3), below-min-notional after clamp (test 5), sub-cap no-op (test 6). §7 tests all present. No gaps.
- **Placeholder scan:** none — full code and exact commands in every step.
- **Type consistency:** clamp uses `RiskConfig.max_order_notional`/`max_position_qty`/`max_gross_exposure`/`rebalance_min_notional` (verified field names) and `snapshot.gross_exposure()`/`position_qty()`; `RebalanceTrade(symbol, side, qty, action, new_total_qty)` matches the existing dataclass; `"CAPPED"` used consistently in code and test.
