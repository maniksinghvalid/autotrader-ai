# Covered-Share Reservation on Rebalance Trims — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stop the midday rebalancer from trimming equity shares that are pledged as cover to an open short call, so a rebalance can never leave a covered call / collar naked.

**Architecture:** Two-file change in the AutoTrader repo. `AccountSnapshot` gains a `short_option_contracts(underlying, right)` method (the canonical option-code counter, relocated from `risk_core` which becomes a thin delegator). `rebalance.compute_plan` caps each TRIM at the covered floor (`short_call_contracts × 100`), doing a partial trim down to the floor.

**Tech Stack:** Python 3, pytest. Pure functions over `AccountSnapshot` — no SDK, no I/O.

## Global Constraints

- **Enforce in `rebalance.compute_plan` only** — no change to `risk_core`'s evaluation logic (`_evaluate` / `_evaluate_option_leg`) or any non-rebalance SELL path.
- **Only open short CALLs pledge shares.** Floor = `short_call_contracts × 100`. Long puts pledge nothing; v1 is long-only equity (no share-pledging short puts).
- **`SHARES_PER_CONTRACT = 100`** (matches `OptionContract.multiplier` default). Non-100 multipliers out of scope.
- **Partial trim to the covered floor** (decision C3): sell `min(desired, current_qty − covered_floor)`; if that is `≤ 0`, skip with reason `COVERED_FLOOR`.
- **TOPUP (BUY) untouched** — buying never strips coverage.
- **DRY (C6):** one source of truth — `AccountSnapshot.short_option_contracts`; `risk_core._short_option_contracts` delegates to it. Existing `risk_core` tests must stay green (parity).
- **No new config; `RISK_REBALANCE_ENABLED` unchanged.**
- **Option-code format:** moomoo codes are `<underlying><6-digit-date><C|P><strike>`, e.g. `US.AAPL260717C210000`. The suffix regex `^\d{6}([CP])\d+$` (group 1 = C/P) both classifies the right AND guards prefix collisions (`US.OXY…`'s residual `"XY26…"` fails to match, so it is not counted for `US.O`).

---

### Task 1: `AccountSnapshot.short_option_contracts` + `risk_core` delegation

**Files:**
- Modify: `autotrader/domain.py` (add `import re`; add module regex `_OPT_SUFFIX_RE`; add method on `AccountSnapshot` after `position_qty`, ~line 172)
- Modify: `autotrader/risk_core.py` (remove `_OPT_SUFFIX` regex + its comment ~lines 14-17; make `_short_option_contracts` delegate ~lines 24-36; remove now-unused `import re` ~line 7)
- Test: `tests/test_domain.py`

**Interfaces:**
- Consumes: `AccountSnapshot.positions` (`Tuple[Position, ...]`, each `Position(symbol: str, qty: int, avg_price: float)`).
- Produces: `AccountSnapshot.short_option_contracts(underlying: str, right: str) -> int` — count of open SHORT contracts (sum of `-qty` over negative-qty positions) on `underlying` for `right` in `{"CALL","PUT"}`. Returns 0 when none. `risk_core._short_option_contracts(snapshot, underlying, right)` keeps the same signature and now returns `snapshot.short_option_contracts(underlying, right)`.

> All commands run from `/Users/acdc/Documents/AI/AutoTrader`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_domain.py`:

```python
def test_short_option_contracts_counts_short_calls_only():
    from autotrader.domain import AccountSnapshot, Position
    snap = AccountSnapshot(
        cash=0.0, total_assets=0.0, day_pnl=0.0, stale=False,
        positions=(
            Position("US.AAPL", 100, 190.0),                 # long stock — ignored
            Position("US.AAPL260821C200000", -2, 3.0),       # short call ×2 — counts
            Position("US.AAPL260821C210000", 1, 1.0),        # long call — ignored
            Position("US.AAPL260821P180000", -1, 2.0),       # short put — not a CALL
            Position("US.MSFT260821C400000", -5, 4.0),       # other underlying — ignored
        ),
    )
    assert snap.short_option_contracts("US.AAPL", "CALL") == 2
    assert snap.short_option_contracts("US.AAPL", "PUT") == 1
    assert snap.short_option_contracts("US.AAPL", "CALL") == 2  # idempotent / no mutation


def test_short_option_contracts_zero_when_none():
    from autotrader.domain import AccountSnapshot, Position
    snap = AccountSnapshot(cash=0.0, total_assets=0.0, day_pnl=0.0, stale=False,
                           positions=(Position("US.AAPL", 100, 190.0),))
    assert snap.short_option_contracts("US.AAPL", "CALL") == 0


def test_short_option_contracts_no_prefix_collision():
    # "US.O" must NOT count "US.OXY" options: the residual "XY26…" fails the
    # ^\d{6}[CP]\d+$ suffix regex.
    from autotrader.domain import AccountSnapshot, Position
    snap = AccountSnapshot(
        cash=0.0, total_assets=0.0, day_pnl=0.0, stale=False,
        positions=(
            Position("US.O260821C50000", -1, 1.0),     # US.O short call ×1
            Position("US.OXY260821C50000", -3, 2.0),   # US.OXY short call — must not count for US.O
        ),
    )
    assert snap.short_option_contracts("US.O", "CALL") == 1
    assert snap.short_option_contracts("US.OXY", "CALL") == 3
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python3 -m pytest tests/test_domain.py -k short_option_contracts -v`
Expected: FAIL with `AttributeError: 'AccountSnapshot' object has no attribute 'short_option_contracts'`.

- [ ] **Step 3: Add the regex + method to `domain.py`**

Add `import re` to the import block (after `import math`). Add the module-level regex just below the imports (near the top, before the first class):

```python
# Suffix of a moomoo option code after the underlying prefix, e.g. for
# "US.AAPL260717C210000" after stripping "US.AAPL" → "260717C210000".
# Group 1 is "C" or "P". The leading \d{6} also guards prefix collisions
# (US.OXY…'s residual "XY26…" fails to match, so it is not counted for US.O).
_OPT_SUFFIX_RE = re.compile(r"^\d{6}([CP])\d+$")
```

Then add this method to `AccountSnapshot`, immediately after `position_qty` (after line 172):

```python
    def short_option_contracts(self, underlying: str, right: str) -> int:
        """Total open SHORT option contracts (sum of -qty over negative-qty
        positions) on `underlying` for the given right ("CALL"/"PUT"), parsed
        from moomoo option codes (e.g. US.AAPL260717C210000). Used both to
        reserve shares pledged to covered calls (so a trim never strips cover)
        and to bound stacked covered shorts in the risk core."""
        want = "C" if right == "CALL" else "P"
        total = 0
        for p in self.positions:
            if p.qty >= 0 or not p.symbol.startswith(underlying):
                continue
            m = _OPT_SUFFIX_RE.match(p.symbol[len(underlying):])
            if m and m.group(1) == want:
                total += -p.qty
        return total
```

- [ ] **Step 4: Run the domain tests to verify they pass**

Run: `python3 -m pytest tests/test_domain.py -k short_option_contracts -v`
Expected: PASS (3 tests).

- [ ] **Step 5: Point `risk_core` at the new method (delegation)**

In `autotrader/risk_core.py`:

a) Replace the `_short_option_contracts` function body. The OLD function is:

```python
def _short_option_contracts(snapshot, underlying: str, right: str) -> int:
    """Total short contracts already open on `underlying` for the given right
    (CALL/PUT), parsed from moomoo option codes (e.g. US.AAPL260717C210000).
    Used to ensure stacked covered calls never become an aggregate naked short."""
    want = "C" if right == "CALL" else "P"
    total = 0
    for p in snapshot.positions:
        if p.qty >= 0 or not p.symbol.startswith(underlying):
            continue
        m = _OPT_SUFFIX.match(p.symbol[len(underlying):])
        if m and m.group(1) == want:
            total += -p.qty
    return total
```

Replace it with:

```python
def _short_option_contracts(snapshot, underlying: str, right: str) -> int:
    """Open short contracts on `underlying` for the given right. Delegates to
    AccountSnapshot.short_option_contracts — the single source of truth."""
    return snapshot.short_option_contracts(underlying, right)
```

b) Delete the now-unused `_OPT_SUFFIX` regex and its comment (the block):

```python
# Matches the suffix of a moomoo option code after the underlying prefix:
# e.g. for "US.AAPL260717C210000" after stripping "US.AAPL" → "260717C210000"
# Group 1 is "C" or "P".
_OPT_SUFFIX = re.compile(r"^\d{6}([CP])\d+$")
```

c) `re` is now unused in `risk_core.py` — remove the `import re` line. (Confirm with `grep -n "re\." autotrader/risk_core.py` returning nothing before removing.)

The call site at ~line 169 (`existing_short = _short_option_contracts(snapshot, underlying, opt.right)`) is unchanged.

- [ ] **Step 6: Verify the full risk_core + domain suites stay green (parity)**

Run: `python3 -m pytest tests/test_domain.py tests/test_risk_core.py tests/test_risk_core_options.py tests/test_risk_core_reduce_only.py -q`
Expected: PASS, no failures — the delegation preserves `risk_core`'s coverage behavior exactly.

- [ ] **Step 7: Commit**

```bash
git add autotrader/domain.py autotrader/risk_core.py tests/test_domain.py
git commit -m "refactor(domain): add AccountSnapshot.short_option_contracts; risk_core delegates

Single source of truth for counting open short option contracts, ahead of
the rebalancer's covered-share reservation."
```

---

### Task 2: Covered-floor cap in `compute_plan`

**Files:**
- Modify: `autotrader/rebalance.py` (add `SHARES_PER_CONTRACT` constant; coverage cap in the TRIM branch of `compute_plan`)
- Test: `tests/test_rebalance.py`

**Interfaces:**
- Consumes: `AccountSnapshot.short_option_contracts(underlying, right) -> int` (Task 1).
- Produces: a new skip reason string `"COVERED_FLOOR"` in `RebalancePlan.skipped`; TRIM `qty` is capped so `new_total_qty >= short_call_contracts × SHARES_PER_CONTRACT`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_rebalance.py` (`AccountSnapshot`, `Position`, `compute_plan`, and `_cfg` are already imported/defined at the top of that file):

```python
def test_covered_position_trims_only_to_floor():
    # 150 shares @ $100 = $15000 (overweight), 1 short call pledges 100 shares.
    # Single-symbol allow-list → target weight 0.90 → target_value 9000 → desired
    # trim 60, but coverage caps the sell at 150-100 = 50 (floor of 100 kept).
    snap = AccountSnapshot(
        cash=0.0, total_assets=10000.0, day_pnl=0.0, stale=False,
        positions=(Position("US.AAPL", 150, 100.0),
                   Position("US.AAPL260821C110000", -1, 2.0)),
    )
    cfg = _cfg(allowed_symbols=frozenset({"US.AAPL"}))
    plan = compute_plan(snap, {"US.AAPL": 100.0}, {"US.AAPL": 100.0}, cfg)
    assert len(plan.trades) == 1
    t = plan.trades[0]
    assert (t.symbol, t.side, t.action) == ("US.AAPL", "SELL", "TRIM")
    assert t.qty == 50 and t.new_total_qty == 100


def test_fully_covered_position_skipped():
    # 100 shares, 1 short call (floor 100). Overweight, but free-to-sell = 0.
    snap = AccountSnapshot(
        cash=0.0, total_assets=10000.0, day_pnl=0.0, stale=False,
        positions=(Position("US.AAPL", 100, 100.0),
                   Position("US.AAPL260821C110000", -1, 2.0)),
    )
    cfg = _cfg(allowed_symbols=frozenset({"US.AAPL"}))
    plan = compute_plan(snap, {"US.AAPL": 100.0}, {"US.AAPL": 100.0}, cfg)
    assert plan.trades == ()
    assert ("US.AAPL", "COVERED_FLOOR") in plan.skipped


def test_coverage_cap_below_min_notional_skipped():
    # 101 shares, 1 short call → free-to-sell = 1 share = $100 < $200 min notional.
    snap = AccountSnapshot(
        cash=0.0, total_assets=10000.0, day_pnl=0.0, stale=False,
        positions=(Position("US.AAPL", 101, 100.0),
                   Position("US.AAPL260821C110000", -1, 2.0)),
    )
    cfg = _cfg(allowed_symbols=frozenset({"US.AAPL"}))
    plan = compute_plan(snap, {"US.AAPL": 100.0}, {"US.AAPL": 100.0}, cfg)
    assert plan.trades == ()
    assert ("US.AAPL", "SKIPPED_MIN_NOTIONAL") in plan.skipped


def test_no_short_calls_trims_unchanged():
    # Regression: with no short call, behavior is identical to before (full trim).
    # 150 shares @ $100, single-symbol target 0.90 → desired trim 60, uncapped.
    snap = AccountSnapshot(
        cash=0.0, total_assets=10000.0, day_pnl=0.0, stale=False,
        positions=(Position("US.AAPL", 150, 100.0),),
    )
    cfg = _cfg(allowed_symbols=frozenset({"US.AAPL"}))
    plan = compute_plan(snap, {"US.AAPL": 100.0}, {"US.AAPL": 100.0}, cfg)
    assert len(plan.trades) == 1
    assert plan.trades[0].qty == 60 and plan.trades[0].new_total_qty == 90
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python3 -m pytest tests/test_rebalance.py -k "covered or coverage or no_short_calls" -v`
Expected: FAIL — `test_covered_position_trims_only_to_floor` expects `qty == 50` but the current code trims the full 60 (no coverage cap); `test_fully_covered_position_skipped` finds a trade instead of a `COVERED_FLOOR` skip.

- [ ] **Step 3: Add the constant and the coverage cap**

In `autotrader/rebalance.py`, add a module constant near the top (after the imports, before `target_fractions`):

```python
SHARES_PER_CONTRACT = 100   # US equity option multiplier (domain default)
```

Then, in `compute_plan`, locate the overweight (TRIM) branch — currently:

```python
        if drift > band:  # overweight -> trim
            qty = int((current_value - target_value) // price)
            qty = min(qty, current_qty)
            if qty <= 0:
                skipped.append((symbol, "WITHIN_BAND"))
                continue
            if qty * price < cfg.rebalance_min_notional:
                skipped.append((symbol, "SKIPPED_MIN_NOTIONAL"))
                continue
            trims.append(RebalanceTrade(symbol, "SELL", qty, "TRIM",
                                        current_qty - qty))
```

Replace it with (inserting the coverage cap after the `WITHIN_BAND` check, before the min-notional check):

```python
        if drift > band:  # overweight -> trim
            qty = int((current_value - target_value) // price)
            qty = min(qty, current_qty)
            if qty <= 0:
                skipped.append((symbol, "WITHIN_BAND"))
                continue
            # Reserve shares pledged to open short calls (covered call / collar):
            # never trim below the covered floor, or the short call goes naked.
            covered_floor = (snapshot.short_option_contracts(symbol, "CALL")
                             * SHARES_PER_CONTRACT)
            qty = min(qty, max(0, current_qty - covered_floor))
            if qty <= 0:
                skipped.append((symbol, "COVERED_FLOOR"))
                continue
            if qty * price < cfg.rebalance_min_notional:
                skipped.append((symbol, "SKIPPED_MIN_NOTIONAL"))
                continue
            trims.append(RebalanceTrade(symbol, "SELL", qty, "TRIM",
                                        current_qty - qty))
```

(The TOPUP / underweight branch is unchanged.)

- [ ] **Step 4: Run the rebalance tests to verify they pass**

Run: `python3 -m pytest tests/test_rebalance.py -k "covered or coverage or no_short_calls" -v`
Expected: PASS (4 tests).

- [ ] **Step 5: Run the full rebalance suite (no regressions)**

Run: `python3 -m pytest tests/test_rebalance.py -q`
Expected: PASS — pre-existing tests (`test_overweight_trims_partial_sell`, `test_within_band_is_skipped`, `test_min_notional_skips_churn`, etc.) still pass, since `covered_floor == 0` for non-covered names leaves the path identical.

- [ ] **Step 6: Commit**

```bash
git add autotrader/rebalance.py tests/test_rebalance.py
git commit -m "feat(rebalance): reserve covered-call shares when trimming

compute_plan caps each TRIM at the covered floor (short_call_contracts*100),
partial-trimming to the floor and skipping COVERED_FLOOR when fully pledged,
so a rebalance can never leave a covered call / collar naked."
```

---

## Update the prior spec's open precondition (not a code task)

After both tasks land, the portfolio-targets spec §9 precondition is satisfied. No code change here — just be aware the remaining gate to live rebalancing is the human-reviewed `RISK_REBALANCE_ENABLED=true`, still out of scope.

## Self-Review

- **Spec coverage:** C1 rebalancer-only → Task 2 (no risk_core eval change). C2 short-calls-only / floor → Task 2 cap uses `short_option_contracts(symbol,"CALL")`. C3 partial trim → Task 2 `qty = min(qty, max(0, current_qty - covered_floor))`, test `test_covered_position_trims_only_to_floor`. C4 `COVERED_FLOOR` skip → Task 2 test `test_fully_covered_position_skipped`. C5 `SHARES_PER_CONTRACT=100` → Task 2 constant. C6 DRY helper + delegator → Task 1. C7 TOPUP untouched → stated; existing `test_underweight_tops_up_buy` guards it. §6 edge cases: no-short-calls (`test_no_short_calls_trims_unchanged`), fully pledged (`test_fully_covered_position_skipped`), partial (`test_covered_position_trims_only_to_floor`), prefix-collision (`test_short_option_contracts_no_prefix_collision`), below-min-notional (`test_coverage_cap_below_min_notional_skipped`). §7 testing → Tasks 1-2. No gaps.
- **Placeholder scan:** none — all steps carry concrete code and exact commands.
- **Type consistency:** `short_option_contracts(underlying: str, right: str) -> int` is defined in Task 1 and consumed identically in Task 2; `SHARES_PER_CONTRACT` defined and used in Task 2; `RebalanceTrade(symbol, side, qty, action, new_total_qty)` matches the existing dataclass.
