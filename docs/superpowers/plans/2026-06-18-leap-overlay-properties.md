# LEAP Overlay Properties Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give the standalone `LEAP` overlay a distinct contract-selection profile (deep-ITM 0.80Δ, 180–730 DTE, prefer-longest in-window, roll at 120 DTE, no profit target) and make long-dated option legs reachable by windowing the chain fetch.

**Architecture:** Five sequenced tasks. (1) Add a `prefer_longest` tie-break to the pure selector. (2) Add the structural fields to `LegSpec`/`OverlayDef`/`ExitRule` (no behavior change). (3) Window the broker chain fetch via an expiration-date-driven helper and thread the DTE window through the `Broker` interface. (4) Wire the planner to honor `prefer_longest` and per-overlay exit overrides. (5) Flip the LEAP REGISTRY values and update its tests. Each task keeps the full suite green.

**Tech Stack:** Python 3, pytest, `moomoo-api` SDK (confined to `moomoo_broker.py`), in-memory `SimBroker` for unit tests.

## Global Constraints

- Default to paper trading (`TrdEnv.SIMULATE`); never call `unlock_trade`. (CLAUDE.md)
- Every Moomoo call checks `ret_code == RET_OK`; log non-OK with context, never swallow. When `ret != RET_OK` the second tuple element is an **error string**, not a DataFrame. (CLAUDE.md)
- All SDK / `moomoo` imports stay confined to `autotrader/moomoo_broker.py` (lazy imports inside methods); the module must stay SDK-free at import time. (existing convention)
- LEAP selection params live in the `overlays.py` REGISTRY (structural params), matching COLLAR / BEAR_PUT_SPREAD / CALL_DIAGONAL. No new `RISK_OPTION_*` config keys. (approved design decision)
- Scope is the standalone `LEAP` overlay only. Do **not** change `CALL_DIAGONAL` (PMCC) selection params.
- `get_market_snapshot` is capped at 400 codes/call; `get_option_chain` rejects any `[start, end]` span exceeding 30 days. (`skills/moomooapi/docs/API_LIMITS.md`, verified live 2026-06-18)
- TDD: write the failing test first, watch it fail, implement minimally, watch it pass, commit.
- Run the suite with: `PYTHONPATH=. python3 -m pytest tests/ -q`

---

### Task 1: `prefer_longest` tie-break in the contract selector

**Files:**
- Modify: `autotrader/options/chain.py:29-47`
- Test: `tests/test_options_chain.py` (create)

**Interfaces:**
- Consumes: `OptionQuote` (existing, `autotrader/options/chain.py:13`).
- Produces: `select_contract(quotes, right, target_delta, dte_min, dte_max, asof, pin_expiry=None, prefer_longest=False) -> Optional[OptionQuote]`. When `prefer_longest=True`, ties after delta-closeness are broken toward the **longest** DTE; default `False` preserves the existing nearest-DTE behavior.

- [ ] **Step 1: Write the failing test**

Create `tests/test_options_chain.py`:

```python
from datetime import date, timedelta

from autotrader.options.chain import OptionQuote, select_contract


def _asof():
    return date(2026, 6, 18)


def _chain():
    a = _asof()
    # Two in-window expiries, both carrying an ~0.80-delta call.
    return [
        OptionQuote("NEAR80", "US.AAPL", a + timedelta(days=200), 180, "CALL", 0.80, 30.0),
        OptionQuote("FAR80", "US.AAPL", a + timedelta(days=600), 175, "CALL", 0.80, 40.0),
        OptionQuote("FAR70", "US.AAPL", a + timedelta(days=600), 190, "CALL", 0.70, 25.0),
    ]


def test_prefer_longest_picks_furthest_in_window_at_target_delta():
    q = select_contract(_chain(), "CALL", 0.80, 180, 730, _asof(), prefer_longest=True)
    assert q.code == "FAR80"


def test_default_prefers_nearest_in_window():
    q = select_contract(_chain(), "CALL", 0.80, 180, 730, _asof())
    assert q.code == "NEAR80"


def test_prefer_longest_still_respects_delta_closeness_first():
    # FAR70 is further from 0.80 than FAR80; delta-closeness must dominate DTE.
    q = select_contract(_chain(), "CALL", 0.80, 180, 730, _asof(), prefer_longest=True)
    assert q.delta == 0.80
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH=. python3 -m pytest tests/test_options_chain.py -q`
Expected: FAIL — `select_contract() got an unexpected keyword argument 'prefer_longest'`.

- [ ] **Step 3: Write minimal implementation**

In `autotrader/options/chain.py`, replace the `select_contract` signature and the final `return min(...)` (lines 29-47):

```python
def select_contract(quotes: List[OptionQuote], right: OptionRight,
                    target_delta: float, dte_min: int, dte_max: int,
                    asof: date, pin_expiry: Optional[date] = None,
                    prefer_longest: bool = False) -> Optional[OptionQuote]:
    """Closest-to-target-|delta| contract of `right` whose DTE is in
    [dte_min, dte_max] and premium > 0. When `pin_expiry` is set, candidates are
    further restricted to that exact expiry (used to keep multi-leg single-expiry
    strategies on one expiry). None if nothing qualifies. Ties after delta-closeness
    break by expiry then lowest strike: nearest expiry by default, or furthest when
    `prefer_longest` is set (LEAP-style long-dated legs)."""
    target = abs(target_delta)
    candidates = [
        q for q in quotes
        if q.right == right and q.premium > 0
        and dte_min <= (q.expiry - asof).days <= dte_max
        and (pin_expiry is None or q.expiry == pin_expiry)
    ]
    if not candidates:
        return None
    dte_sign = -1 if prefer_longest else 1
    return min(candidates, key=lambda q: (abs(abs(q.delta) - target),
                                          dte_sign * (q.expiry - asof).days,
                                          q.strike))
```

- [ ] **Step 4: Run test to verify it passes**

Run: `PYTHONPATH=. python3 -m pytest tests/test_options_chain.py -q`
Expected: PASS (3 passed).

- [ ] **Step 5: Run the full suite (no regressions)**

Run: `PYTHONPATH=. python3 -m pytest tests/ -q`
Expected: all pass (existing callers use the `prefer_longest=False` default).

- [ ] **Step 6: Commit**

```bash
git add autotrader/options/chain.py tests/test_options_chain.py
git commit -m "feat(options): add prefer_longest tie-break to select_contract

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 2: Structural fields on `LegSpec` / `OverlayDef` / `ExitRule`

**Files:**
- Modify: `autotrader/options/overlays.py:14-64`
- Test: `tests/test_overlays_registry.py` (create)

**Interfaces:**
- Produces:
  - `LegSpec.prefer_longest: bool = False`
  - Module sentinel `_UNSET` and `OverlayDef.dte_to_close` / `OverlayDef.profit_target_pct`, both defaulting to `_UNSET` ("not overridden"). A value of `None` is a valid explicit override (e.g. profit target disabled).
  - `ExitRule.profit_target_pct: Optional[float]` (`None` = no profit target).
- Note: no behavior change yet — the planner does not read these until Task 4. The LEAP REGISTRY values are unchanged in this task.

- [ ] **Step 1: Write the failing test**

Create `tests/test_overlays_registry.py`:

```python
from autotrader.options.overlays import (
    ExitRule, LegSpec, OverlayDef, _UNSET,
)


def test_legspec_prefer_longest_defaults_false():
    leg = LegSpec(right="CALL", side="BUY")
    assert leg.prefer_longest is False


def test_legspec_prefer_longest_settable():
    leg = LegSpec(right="CALL", side="BUY", prefer_longest=True)
    assert leg.prefer_longest is True


def test_overlaydef_exit_overrides_default_unset():
    d = OverlayDef(requires_underlying=False, legs=(LegSpec(right="CALL", side="BUY"),))
    assert d.dte_to_close is _UNSET
    assert d.profit_target_pct is _UNSET


def test_overlaydef_exit_overrides_accept_none_and_values():
    d = OverlayDef(requires_underlying=False,
                   legs=(LegSpec(right="CALL", side="BUY"),),
                   dte_to_close=120, profit_target_pct=None)
    assert d.dte_to_close == 120
    assert d.profit_target_pct is None


def test_exitrule_profit_target_optional():
    r = ExitRule(dte_to_close=120, profit_target_pct=None)
    assert r.profit_target_pct is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH=. python3 -m pytest tests/test_overlays_registry.py -q`
Expected: FAIL — `cannot import name '_UNSET'` (and unknown kwargs).

- [ ] **Step 3: Write minimal implementation**

In `autotrader/options/overlays.py`:

a) Add the sentinel just below the imports (after line 11):

```python
_UNSET = object()  # "not overridden" marker; None is a valid explicit override
```

b) Add the field to `LegSpec` (after line 29, `dte_max: Optional[int] = None`):

```python
    prefer_longest: bool = False
```

c) Change `ExitRule.profit_target_pct` (line 56) to Optional:

```python
    profit_target_pct: Optional[float]
```

d) Add the override fields to `OverlayDef` (the dataclass at lines 60-64):

```python
@dataclass(frozen=True)
class OverlayDef:
    requires_underlying: bool
    legs: Tuple[LegSpec, ...]
    single_expiry: bool = False
    dte_to_close: object = _UNSET          # int override, or _UNSET to use cfg
    profit_target_pct: object = _UNSET     # float|None override, or _UNSET to use cfg
```

- [ ] **Step 4: Run test to verify it passes**

Run: `PYTHONPATH=. python3 -m pytest tests/test_overlays_registry.py -q`
Expected: PASS (5 passed).

- [ ] **Step 5: Run the full suite (no regressions)**

Run: `PYTHONPATH=. python3 -m pytest tests/ -q`
Expected: all pass (new fields are additive; planner unchanged).

- [ ] **Step 6: Commit**

```bash
git add autotrader/options/overlays.py tests/test_overlays_registry.py
git commit -m "feat(options): add prefer_longest + per-overlay exit-override fields

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 3: Window the chain fetch through the `Broker` interface

**Files:**
- Modify: `autotrader/broker.py:23-27`
- Modify: `autotrader/sim_broker.py:39-40`
- Modify: `autotrader/moomoo_broker.py:89-175`
- Modify: `autotrader/options/planner.py:100`
- Test: `tests/test_chain_windows.py` (create)

**Interfaces:**
- Consumes: nothing new.
- Produces:
  - `Broker.get_option_chain(self, underlying, right, dte_min=0, dte_max=100000)` — new DTE-window params (defaults keep standalone/sim callers working).
  - Module helper `autotrader.moomoo_broker._chain_windows(expiries, today, dte_min, dte_max, max_span=30) -> List[Tuple[date, date]]` — pure; groups in-window expiries into ≤`max_span`-day `(start, end)` windows. Importable without OpenD.

- [ ] **Step 1: Write the failing test (pure windowing helper)**

Create `tests/test_chain_windows.py`:

```python
from datetime import date, timedelta

from autotrader.moomoo_broker import _chain_windows


def _today():
    return date(2026, 6, 18)


def test_filters_to_dte_window():
    t = _today()
    expiries = [t + timedelta(days=10),   # below dte_min
                t + timedelta(days=200),  # in window
                t + timedelta(days=900)]  # above dte_max
    wins = _chain_windows(expiries, t, 180, 730)
    assert wins == [(t + timedelta(days=200), t + timedelta(days=200))]


def test_groups_nearby_expiries_within_max_span():
    t = _today()
    expiries = [t + timedelta(days=200), t + timedelta(days=220),
                t + timedelta(days=400)]
    wins = _chain_windows(expiries, t, 180, 730, max_span=30)
    # 200 and 220 are within 30 days -> one window; 400 is its own.
    assert wins == [(t + timedelta(days=200), t + timedelta(days=220)),
                    (t + timedelta(days=400), t + timedelta(days=400))]


def test_empty_when_nothing_in_window():
    t = _today()
    assert _chain_windows([t + timedelta(days=5)], t, 180, 730) == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH=. python3 -m pytest tests/test_chain_windows.py -q`
Expected: FAIL — `cannot import name '_chain_windows'`.

- [ ] **Step 3a: Add the pure helper to `moomoo_broker.py`**

Add at module level in `autotrader/moomoo_broker.py` (after the `_STATUS_MAP` block, before `class MoomooBroker`):

```python
def _chain_windows(expiries, today, dte_min, dte_max, max_span=30):
    """Group expiries that fall within [today+dte_min, today+dte_max] into
    <=max_span-day (start, end) windows, so each get_option_chain call stays
    under the 30-day span cap while covering only real expiries (not blank
    calendar). Pure: no SDK, importable without OpenD."""
    from datetime import date as _date  # noqa: F401 — typing clarity only
    qualifying = sorted(e for e in expiries
                        if dte_min <= (e - today).days <= dte_max)
    windows = []
    i = 0
    while i < len(qualifying):
        start = qualifying[i]
        j = i
        while j + 1 < len(qualifying) and (qualifying[j + 1] - start).days <= max_span:
            j += 1
        windows.append((start, qualifying[j]))
        i = j + 1
    return windows
```

- [ ] **Step 3b: Rewrite `MoomooBroker.get_option_chain` to be window-driven**

Replace the whole method body (`autotrader/moomoo_broker.py:89-175`) with:

```python
    def get_option_chain(self, underlying: str, right,
                         dte_min: int = 0, dte_max: int = 100000):  # pragma: no cover — live OpenD
        """Live option chain for `underlying` (e.g. US.AAPL) and `right`
        (CALL/PUT), restricted to expiries in [today+dte_min, today+dte_max],
        returned as options.chain.OptionQuote rows.

        get_option_chain rejects any [start, end] span > 30 days, so we enumerate
        expiries once via get_option_expiration_date (no span limit), keep only
        those in the DTE window, and fetch the chain per <=30-day expiry bucket.
        All SDK/options imports are confined here (module stays SDK-free at import).
        Field names per vendored get_option_chain.py / get_option_expiration_date.py;
        safe_get tolerates the candidate keys below across SDK versions."""
        from datetime import date as _date, datetime
        from autotrader.options.chain import OptionQuote
        from moomoo import OptionType  # confined import

        opt_type = OptionType.CALL if str(right).upper() == "CALL" else OptionType.PUT
        today = _date.today()

        eret, edf = self._quote.get_option_expiration_date(underlying)
        if not self._ok(eret) or self._c.is_empty(edf):
            logger.info("get_option_expiration_date %s: ret=%s %s", underlying, eret,
                        edf if isinstance(edf, str) else "empty")
            return []
        expiries = []
        for i in range(len(edf)):
            s = str(self._c.safe_get(edf.iloc[i], "strike_time", "expiry_date", default=""))
            try:
                expiries.append(datetime.strptime(s[:10], "%Y-%m-%d").date())
            except (ValueError, TypeError):
                continue

        codes: List[str] = []
        seen = set()
        for w_start, w_end in _chain_windows(expiries, today, dte_min, dte_max):
            ret, chain = self._quote.get_option_chain(
                underlying, option_type=opt_type,
                start=w_start.strftime("%Y-%m-%d"), end=w_end.strftime("%Y-%m-%d"))
            if not self._ok(ret) or self._c.is_empty(chain):
                # A bad sub-window must not abort the others; the error payload is
                # a str (not a df) when ret != RET_OK.
                logger.info("get_option_chain %s %s %s..%s: ret=%s %s",
                            underlying, str(right).upper(), w_start, w_end, ret,
                            chain if isinstance(chain, str) else "empty")
                continue
            for i in range(len(chain)):
                code = str(self._c.safe_get(chain.iloc[i], "code", default=""))
                if code and code not in seen:
                    seen.add(code)
                    codes.append(code)
        if not codes:
            return []

        # get_market_snapshot is capped at 400 codes per call (API_LIMITS.md), and
        # liquid names easily exceed that (AAPL ~768 puts), so batch the request.
        SNAPSHOT_MAX = 400
        out = []
        for start in range(0, len(codes), SNAPSHOT_MAX):
            batch = codes[start:start + SNAPSHOT_MAX]
            sret, snap = self._quote.get_market_snapshot(batch)
            if not self._ok(sret) or self._c.is_empty(snap):
                logger.info("get_market_snapshot %s batch[%d:%d]: ret=%s %s",
                            underlying, start, start + len(batch), sret,
                            snap if isinstance(snap, str) else "empty")
                continue
            for i in range(len(snap)):
                row = snap.iloc[i]
                code = str(self._c.safe_get(row, "code", default=""))
                strike = self._c.safe_float(self._c.safe_get(
                    row, "option_strike_price", "strike_price", "strike", default=0))
                exp = str(self._c.safe_get(
                    row, "option_expiry_date", "strike_time", "expiry_date", default=""))
                try:
                    expiry = datetime.strptime(exp[:10], "%Y-%m-%d").date()
                except (ValueError, TypeError):
                    continue
                bid = self._c.safe_float(self._c.safe_get(row, "bid_price", "bid", default=0))
                ask = self._c.safe_float(self._c.safe_get(row, "ask_price", "ask", default=0))
                mid = (bid + ask) / 2 if (bid and ask) else self._c.safe_float(
                    self._c.safe_get(row, "last_price", "cur_price", default=0))
                delta = self._c.safe_float(self._c.safe_get(
                    row, "option_delta", "delta", default=0))
                if strike <= 0 or mid <= 0:
                    continue
                out.append(OptionQuote(code=code, underlying=underlying, expiry=expiry,
                                       strike=strike, right=str(right).upper(),
                                       delta=delta, premium=mid))
        return out
```

- [ ] **Step 3c: Update the `Broker` ABC signature**

In `autotrader/broker.py` replace lines 23-27:

```python
    def get_option_chain(self, underlying: str, right: OptionRight,
                         dte_min: int = 0, dte_max: int = 100000) -> List["OptionQuote"]:
        """Return chain rows (strike/expiry/delta/premium) for one right, limited
        to expiries within [today+dte_min, today+dte_max]. Live impl is
        MoomooBroker; base raises so a broker without it fails loud."""
        raise NotImplementedError
```

- [ ] **Step 3d: Update `SimBroker` signature (window ignored; canned data)**

In `autotrader/sim_broker.py` replace lines 39-40:

```python
    def get_option_chain(self, underlying: str, right: OptionRight,
                         dte_min: int = 0, dte_max: int = 100000) -> List[OptionQuote]:
        # Window args accepted for interface parity; the precise DTE filter runs
        # in select_contract over the canned chain.
        return list(self._chains.get((underlying.upper(), right.upper()), []))
```

- [ ] **Step 3e: Pass the window from the planner**

In `autotrader/options/planner.py` replace line 100:

```python
        quotes = chain_provider.get_option_chain(underlying, spec.right, dte_min, dte_max)
```

- [ ] **Step 4: Run tests**

Run: `PYTHONPATH=. python3 -m pytest tests/test_chain_windows.py tests/test_options_planner.py -q`
Expected: PASS (windowing helper + existing planner tests; sim ignores the window, select_contract still filters).

- [ ] **Step 5: Run the full suite (no regressions)**

Run: `PYTHONPATH=. python3 -m pytest tests/ -q`
Expected: all pass.

- [ ] **Step 6: Commit**

```bash
git add autotrader/broker.py autotrader/sim_broker.py autotrader/moomoo_broker.py autotrader/options/planner.py tests/test_chain_windows.py
git commit -m "feat(broker): window option-chain fetch by DTE via expiration-date enumeration

Replaces the blind 90-day chunk with a get_option_expiration_date-driven fetch so
long-dated legs (LEAP 180-730, PMCC long 180-365) are reachable. Threads dte_min/
dte_max through the Broker interface.

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 4: Planner honors `prefer_longest` and per-overlay exit overrides

**Files:**
- Modify: `autotrader/options/planner.py:96-123`
- Test: `tests/test_options_planner.py` (add cases)

**Interfaces:**
- Consumes: `LegSpec.prefer_longest`, `OverlayDef.dte_to_close`, `OverlayDef.profit_target_pct`, `_UNSET` (Task 2); `select_contract(..., prefer_longest=)` (Task 1).
- Produces: no signature change to `build_overlay_plan`; it now passes each leg's `prefer_longest` into `select_contract` and builds `ExitRule` from the overlay's overrides (falling back to `cfg` when `_UNSET`).
- Note: behavior is unchanged for every existing overlay because their `prefer_longest` is `False` and their exit overrides are `_UNSET`. LEAP values change in Task 5.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_options_planner.py` (a covered call must still report the global exit, proving the `_UNSET` fallback path):

```python
def test_existing_overlay_exit_falls_back_to_global_cfg():
    plan = build_overlay_plan(_sig(OverlayType.COVERED_CALL), _snap(200),
                              _broker(), _cfg(), "sig-x", _asof())
    assert isinstance(plan, OverlayPlan)
    assert plan.exit.dte_to_close == 7          # cfg.option_dte_to_close fallback
    assert plan.exit.profit_target_pct == 0.5   # cfg.option_profit_target_pct fallback
```

- [ ] **Step 2: Run test to verify it fails or passes for the right reason**

Run: `PYTHONPATH=. python3 -m pytest tests/test_options_planner.py::test_existing_overlay_exit_falls_back_to_global_cfg -v`
Expected: PASS already (current planner always uses cfg). This is the regression guard for the refactor below — keep it green through Step 3.

- [ ] **Step 3: Wire `prefer_longest` and the exit override**

In `autotrader/options/planner.py`:

a) Update the import (line 14) to pull in the sentinel:

```python
from autotrader.options.overlays import ExitRule, REGISTRY, _UNSET
```

b) Pass `prefer_longest` into the selector. Replace lines 100-102:

```python
        quotes = chain_provider.get_option_chain(underlying, spec.right, dte_min, dte_max)
        q = select_contract(quotes, spec.right, target_delta, dte_min, dte_max,
                            asof, pin_expiry=pin, prefer_longest=spec.prefer_longest)
```

c) Build the `ExitRule` from the overlay's overrides. Replace lines 122-123:

```python
    dtc = (deff.dte_to_close if deff.dte_to_close is not _UNSET
           else cfg.option_dte_to_close)
    ptp = (deff.profit_target_pct if deff.profit_target_pct is not _UNSET
           else cfg.option_profit_target_pct)
    exit_rule = ExitRule(dte_to_close=dtc, profit_target_pct=ptp)
```

- [ ] **Step 4: Run tests**

Run: `PYTHONPATH=. python3 -m pytest tests/test_options_planner.py -q`
Expected: all pass (fallback guard green; other overlays unchanged).

- [ ] **Step 5: Run the full suite (no regressions)**

Run: `PYTHONPATH=. python3 -m pytest tests/ -q`
Expected: all pass.

- [ ] **Step 6: Commit**

```bash
git add autotrader/options/planner.py tests/test_options_planner.py
git commit -m "feat(options): planner honors prefer_longest and per-overlay exit overrides

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 5: Flip the LEAP REGISTRY to the new profile

**Files:**
- Modify: `autotrader/options/overlays.py:102-108`
- Test: `tests/test_options_planner.py` (update one case, add two)

**Interfaces:**
- Consumes: everything from Tasks 1-4.
- Produces: the `LEAP` overlay now selects a 0.80Δ call, 180–730 DTE, prefer-longest, with a `dte_to_close=120` / no-profit-target exit declaration.

- [ ] **Step 1: Update the existing LEAP test and add coverage**

In `tests/test_options_planner.py`:

a) Add a richer chain helper with **two in-window LEAP expiries** so prefer-longest is observable. Add near `_rich_chains` (after line 162):

```python
def _leap_chains():
    a = _asof()
    mid, far = a + timedelta(days=300), a + timedelta(days=600)  # both in 180-730
    return {("US.AAPL", "CALL"): [
        OptionQuote("US.AAPL_MID80", "US.AAPL", mid, 180, "CALL", 0.80, 30.0),
        OptionQuote("US.AAPL_FAR80", "US.AAPL", far, 175, "CALL", 0.80, 40.0),
        OptionQuote("US.AAPL_FAR70", "US.AAPL", far, 195, "CALL", 0.70, 25.0),
    ]}


def _leap_broker():
    return SimBroker(quotes={"US.AAPL": 200.0}, option_chains=_leap_chains())
```

b) Replace `test_leap_plan_single_long_call_sized_by_default_contracts` (lines 213-220) with the prefer-longest + 0.80Δ assertions:

```python
def test_leap_plan_picks_longest_in_window_080_delta_call():
    plan = build_overlay_plan(_sig(OverlayType.LEAP), _snap(0),
                              _leap_broker(), _cfgN(option_default_contracts=2), "s", _asof())
    assert isinstance(plan, OverlayPlan)
    assert len(plan.legs) == 1
    leg = plan.legs[0].request
    assert leg.side == "BUY" and leg.option.right == "CALL"
    assert leg.option.code == "US.AAPL_FAR80"   # 0.80 delta, furthest in window
    assert leg.qty == 2                          # option_default_contracts


def test_leap_exit_rule_rolls_at_120_no_profit_target():
    plan = build_overlay_plan(_sig(OverlayType.LEAP), _snap(0),
                              _leap_broker(), _cfgN(), "s", _asof())
    assert isinstance(plan, OverlayPlan)
    assert plan.exit.dte_to_close == 120
    assert plan.exit.profit_target_pct is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `PYTHONPATH=. python3 -m pytest tests/test_options_planner.py -q`
Expected: FAIL — current LEAP is 0.70Δ / 180-365 / nearest, exit from cfg (7, 0.5).

- [ ] **Step 3: Update the LEAP REGISTRY entry**

In `autotrader/options/overlays.py` replace the `OverlayType.LEAP` entry (lines 102-108):

```python
    OverlayType.LEAP: OverlayDef(
        requires_underlying=False,
        single_expiry=False,
        dte_to_close=120,            # roll ~4 months out (below the 180 buy floor)
        profit_target_pct=None,      # long stock-replacement: ride it
        legs=(
            LegSpec(right="CALL", side="BUY", target_delta=0.80,
                    dte_min=180, dte_max=730, prefer_longest=True),
        ),
    ),
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `PYTHONPATH=. python3 -m pytest tests/test_options_planner.py -q`
Expected: PASS.

- [ ] **Step 5: Run the full suite (no regressions)**

Run: `PYTHONPATH=. python3 -m pytest tests/ -q`
Expected: all pass.

- [ ] **Step 6: Commit**

```bash
git add autotrader/options/overlays.py tests/test_options_planner.py
git commit -m "feat(options): LEAP profile 0.80 delta, 180-730 DTE, prefer-longest, roll@120

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Optional post-merge: live smoke (needs OpenD)

Not a unit task — run manually to confirm against live paper data, mirroring the 2026-06-18 verification:

```bash
PYTHONPATH=. python3 - <<'PY'
from datetime import date
from autotrader.moomoo_broker import MoomooBroker
from autotrader.options.chain import select_contract
b = MoomooBroker(); b.connect(); asof = date.today()
for und in ["US.SPCE", "US.AAPL"]:
    qs = b.get_option_chain(und, "CALL", 180, 730)
    q = select_contract(qs, "CALL", 0.80, 180, 730, asof, prefer_longest=True) if qs else None
    print(und, "LEAP ->", (q.code, q.strike, (q.expiry - asof).days, round(q.delta, 3)) if q else None)
PY
```
Expected: AAPL resolves a ~0.80Δ call near the 730 cap; SPCE resolves its best available long-dated call (or logs a clean skip if none in window).

## Self-Review

- **Spec coverage:** delta 0.80 (Task 5) · 180–730 window (Task 5) · prefer-longest selector + LEAP-only flag (Tasks 1, 5) · roll 120 + no profit target via per-overlay override + sentinel (Tasks 2, 4, 5) · fetch path windowed via expiration-date enumeration + 400-batch snapshot (Task 3) · PMCC params untouched (no task modifies `CALL_DIAGONAL`; reachability restored by Task 3) · REGISTRY hardcode, no config keys (Task 5) · O4 enforcement out of scope (declaration only — Task 4 builds the `ExitRule`, nothing consumes it). All covered.
- **Placeholder scan:** none — every code/test step is concrete.
- **Type consistency:** `prefer_longest: bool` and `select_contract(..., prefer_longest=False)` match across Tasks 1/4/5; `_UNSET` defined in Task 2 and imported in Task 4; `_chain_windows` signature matches between Task 3 helper, its test, and the caller; `ExitRule(dte_to_close, profit_target_pct)` consistent; `get_option_chain(..., dte_min, dte_max)` consistent across ABC/sim/moomoo/planner.
```
