# Options Phantom Strategies Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Register and wire the four declared-but-unimplemented option strategies — collar, bear-put debit spread, call diagonal (PMCC), and long-call LEAP — into the existing overlay pipeline under the current paper-only / single-audited-path safety model.

**Architecture:** Strategies stay declarative data in `overlays.REGISTRY`. The planner gains per-leg delta/DTE targeting + a same-expiry anchor and a non-share-covered sizing path. The risk core learns **defined-risk coverage**: a short leg is approved when a long leg in the same plan (by right/strike/expiry) covers it, in addition to share coverage. The engine passes those long legs as coverage context, submits long-first, and leaves a loud long-only residual if a later leg fails.

**Tech Stack:** Python 3, pytest, pydantic (signal schema), the existing `autotrader/options/*` + `risk_core` + `main` modules. No new dependencies. Tests run offline against `SimBroker`.

**Spec:** `docs/superpowers/specs/2026-06-16-options-phantom-strategies-design.md`

**Run the suite with:** `python3 -m pytest -q` (full offline suite; baseline is green on this branch).

---

### Task 1: Add `LEAP` to the overlay enum and signal schema

**Files:**
- Modify: `autotrader/domain.py:18-26` (`OverlayType`)
- Modify: `autotrader/signals/schema.py:23-26` (`SignalChange.overlay` Literal)
- Test: `tests/test_options_overlays.py`, `tests/test_options_chain.py`

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_options_overlays.py`:

```python
def test_leap_enum_value_exists():
    assert OverlayType.LEAP.value == "LEAP"
```

Add to `tests/test_options_chain.py` (it already imports `OverlayType`, `RoutineSignalPayload`, `normalize_payload`):

```python
def test_schema_and_normalize_accept_leap():
    payload = RoutineSignalPayload(
        routine_id="r1",
        timestamp="2026-06-16T00:00:00Z",
        signal_changes=[{
            "ticker": "US.AAPL", "direction": "UP", "points_delta": 1,
            "overlay": "LEAP",
        }],
    )
    sig = normalize_payload(payload)[0]
    assert sig.overlay is OverlayType.LEAP
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python3 -m pytest tests/test_options_overlays.py::test_leap_enum_value_exists tests/test_options_chain.py::test_schema_and_normalize_accept_leap -v`
Expected: FAIL — `AttributeError: LEAP` / pydantic validation error on `"LEAP"`.

- [ ] **Step 3: Add the enum value**

In `autotrader/domain.py`, add to `OverlayType` (after `BEAR_PUT_SPREAD`):

```python
    BEAR_PUT_SPREAD = "BEAR_PUT_SPREAD"
    LEAP = "LEAP"
```

- [ ] **Step 4: Add `LEAP` to the schema Literal**

In `autotrader/signals/schema.py`, extend the `overlay` Literal:

```python
    overlay: Optional[Literal[
        "COVERED_CALL", "PROTECTIVE_PUT", "COLLAR",
        "CALL_DIAGONAL", "BEAR_PUT_SPREAD", "LEAP",
    ]] = None
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `python3 -m pytest tests/test_options_overlays.py::test_leap_enum_value_exists tests/test_options_chain.py::test_schema_and_normalize_accept_leap -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add autotrader/domain.py autotrader/signals/schema.py tests/test_options_overlays.py tests/test_options_chain.py
git commit -m "feat(options): add LEAP overlay enum value + schema literal"
```

---

### Task 2: Extend `LegSpec` with per-leg targets and `OverlayDef.single_expiry`

**Files:**
- Modify: `autotrader/options/overlays.py:14-46`
- Test: `tests/test_options_overlays.py`

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_options_overlays.py`:

```python
def test_legspec_accepts_per_leg_targets():
    leg = LegSpec(right="CALL", side="BUY", target_delta=0.80, dte_min=180, dte_max=365)
    assert leg.target_delta == 0.80 and leg.dte_min == 180 and leg.dte_max == 365


def test_legspec_targets_default_to_none():
    leg = LegSpec(right="CALL", side="SELL")
    assert leg.target_delta is None and leg.dte_min is None and leg.dte_max is None


def test_legspec_rejects_bad_target_delta():
    import pytest
    with pytest.raises(ValueError):
        LegSpec(right="CALL", side="BUY", target_delta=1.5)


def test_legspec_rejects_inverted_dte():
    import pytest
    with pytest.raises(ValueError):
        LegSpec(right="CALL", side="BUY", dte_min=90, dte_max=30)


def test_overlaydef_single_expiry_defaults_false():
    d = OverlayDef(requires_underlying=True, legs=(LegSpec(right="CALL", side="SELL"),))
    assert d.single_expiry is False
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python3 -m pytest tests/test_options_overlays.py -k "per_leg or default_to_none or bad_target or inverted_dte or single_expiry" -v`
Expected: FAIL — `TypeError: unexpected keyword argument 'target_delta'` / `'single_expiry'`.

- [ ] **Step 3: Extend `LegSpec` and `OverlayDef`**

Replace the `LegSpec` and `OverlayDef` definitions in `autotrader/options/overlays.py` with:

```python
from typing import Dict, Optional, Tuple


@dataclass(frozen=True)
class LegSpec:
    """One leg of an overlay, pre-contract-resolution. `ratio` is contracts per
    100 shares of underlying (1 = one contract per round lot). target_delta /
    dte_min / dte_max are per-leg selection overrides; when None the planner
    falls back to the global config (cfg.option_target_delta / dte_min / dte_max)."""
    right: OptionRight
    side: Side
    position_effect: PositionEffect = "OPEN"
    ratio: int = 1
    target_delta: Optional[float] = None
    dte_min: Optional[int] = None
    dte_max: Optional[int] = None

    def __post_init__(self):
        if self.right not in ("CALL", "PUT"):
            raise ValueError(f"LegSpec.right must be CALL/PUT, got {self.right!r}")
        if self.side not in ("BUY", "SELL"):
            raise ValueError(f"LegSpec.side must be BUY/SELL, got {self.side!r}")
        if self.position_effect not in ("OPEN", "CLOSE"):
            raise ValueError(f"LegSpec.position_effect must be OPEN/CLOSE, got {self.position_effect!r}")
        if self.ratio < 1:
            raise ValueError(f"LegSpec.ratio must be >= 1, got {self.ratio}")
        if self.target_delta is not None and not (0.0 < self.target_delta <= 1.0):
            raise ValueError(f"LegSpec.target_delta must be in (0,1], got {self.target_delta}")
        if (self.dte_min is not None and self.dte_max is not None
                and self.dte_min > self.dte_max):
            raise ValueError(f"LegSpec dte_min {self.dte_min} > dte_max {self.dte_max}")
        if self.dte_min is not None and self.dte_min < 0:
            raise ValueError(f"LegSpec.dte_min must be >= 0, got {self.dte_min}")


@dataclass(frozen=True)
class OverlayDef:
    requires_underlying: bool
    legs: Tuple[LegSpec, ...]
    single_expiry: bool = False
```

Note: the existing top-of-file import is `from typing import Dict, Tuple` — replace it with the `Dict, Optional, Tuple` line shown above (or merge `Optional` in).

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python3 -m pytest tests/test_options_overlays.py -v`
Expected: PASS (all, including the pre-existing O1 tests).

- [ ] **Step 5: Commit**

```bash
git add autotrader/options/overlays.py tests/test_options_overlays.py
git commit -m "feat(options): per-leg delta/DTE overrides + single_expiry on overlay defs"
```

---

### Task 3: Register the four strategies

**Files:**
- Modify: `autotrader/options/overlays.py:49-58` (`REGISTRY`)
- Test: `tests/test_options_overlays.py` (add new + fix the now-stale absence test)

- [ ] **Step 1: Write/adjust the failing tests**

In `tests/test_options_overlays.py`, **replace** `test_unsupported_overlays_absent_from_registry` (it asserted the opposite of what we now want) with:

```python
def test_phantom_strategies_now_registered():
    for ov in (OverlayType.COLLAR, OverlayType.CALL_DIAGONAL,
               OverlayType.BEAR_PUT_SPREAD, OverlayType.LEAP):
        assert ov in REGISTRY


def test_collar_is_long_put_then_short_call_single_expiry():
    d = REGISTRY[OverlayType.COLLAR]
    assert d.requires_underlying is True and d.single_expiry is True
    assert [(l.right, l.side) for l in d.legs] == [("PUT", "BUY"), ("CALL", "SELL")]


def test_bear_put_spread_is_long_then_short_put_single_expiry():
    d = REGISTRY[OverlayType.BEAR_PUT_SPREAD]
    assert d.requires_underlying is False and d.single_expiry is True
    assert [(l.right, l.side) for l in d.legs] == [("PUT", "BUY"), ("PUT", "SELL")]
    assert d.legs[0].target_delta == 0.45 and d.legs[1].target_delta == 0.25


def test_call_diagonal_is_long_far_then_short_near_call():
    d = REGISTRY[OverlayType.CALL_DIAGONAL]
    assert d.requires_underlying is False and d.single_expiry is False
    assert [(l.right, l.side) for l in d.legs] == [("CALL", "BUY"), ("CALL", "SELL")]
    assert d.legs[0].dte_min == 180 and d.legs[1].dte_max == 45


def test_leap_is_single_long_call():
    d = REGISTRY[OverlayType.LEAP]
    assert d.requires_underlying is False and len(d.legs) == 1
    leg = d.legs[0]
    assert leg.right == "CALL" and leg.side == "BUY" and leg.dte_min == 180
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python3 -m pytest tests/test_options_overlays.py -k "phantom or collar or bear_put or diagonal or leap_is_single" -v`
Expected: FAIL — overlays not in `REGISTRY`.

- [ ] **Step 3: Register the strategies**

Replace the `REGISTRY` dict in `autotrader/options/overlays.py` with (keep the two existing O1 entries, add four):

```python
REGISTRY: Dict[OverlayType, OverlayDef] = {
    OverlayType.COVERED_CALL: OverlayDef(
        requires_underlying=True,
        legs=(LegSpec(right="CALL", side="SELL"),),
    ),
    OverlayType.PROTECTIVE_PUT: OverlayDef(
        requires_underlying=True,
        legs=(LegSpec(right="PUT", side="BUY"),),
    ),
    # Anchor leg (legs[0]) is always the covering LONG leg; it is selected first
    # and submitted first. single_expiry pins later legs to the anchor's expiry.
    OverlayType.COLLAR: OverlayDef(
        requires_underlying=True,
        single_expiry=True,
        legs=(
            LegSpec(right="PUT", side="BUY", target_delta=0.30, dte_min=30, dte_max=45),
            LegSpec(right="CALL", side="SELL", target_delta=0.30, dte_min=30, dte_max=45),
        ),
    ),
    OverlayType.BEAR_PUT_SPREAD: OverlayDef(
        requires_underlying=False,
        single_expiry=True,
        legs=(
            LegSpec(right="PUT", side="BUY", target_delta=0.45, dte_min=30, dte_max=45),
            LegSpec(right="PUT", side="SELL", target_delta=0.25, dte_min=30, dte_max=45),
        ),
    ),
    OverlayType.CALL_DIAGONAL: OverlayDef(
        requires_underlying=False,
        single_expiry=False,
        legs=(
            LegSpec(right="CALL", side="BUY", target_delta=0.80, dte_min=180, dte_max=365),
            LegSpec(right="CALL", side="SELL", target_delta=0.30, dte_min=30, dte_max=45),
        ),
    ),
    OverlayType.LEAP: OverlayDef(
        requires_underlying=False,
        single_expiry=False,
        legs=(
            LegSpec(right="CALL", side="BUY", target_delta=0.70, dte_min=180, dte_max=365),
        ),
    ),
}
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python3 -m pytest tests/test_options_overlays.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add autotrader/options/overlays.py tests/test_options_overlays.py
git commit -m "feat(options): register collar/bear-put-spread/call-diagonal/LEAP overlays"
```

---

### Task 4: `select_contract` gains `pin_expiry`

**Files:**
- Modify: `autotrader/options/chain.py:29-45`
- Test: `tests/test_options_chain.py`

- [ ] **Step 1: Write the failing test**

Add to `tests/test_options_chain.py` (it imports `date`; add `timedelta` and `select_contract`/`OptionQuote` imports at top if not present):

```python
from datetime import timedelta
from autotrader.options.chain import OptionQuote, select_contract


def _two_expiry_calls():
    a = date(2026, 6, 16)
    near, far = a + timedelta(days=35), a + timedelta(days=300)
    return [
        OptionQuote("C-NEAR", "US.AAPL", near, 210, "CALL", 0.30, 1.5),
        OptionQuote("C-FAR", "US.AAPL", far, 210, "CALL", 0.30, 20.0),
    ]


def test_select_contract_pin_expiry_filters_to_that_expiry():
    a = date(2026, 6, 16)
    near = a + timedelta(days=35)
    q = select_contract(_two_expiry_calls(), "CALL", 0.30, 0, 400, a, pin_expiry=near)
    assert q.code == "C-NEAR"


def test_select_contract_pin_expiry_none_is_unchanged():
    a = date(2026, 6, 16)
    q = select_contract(_two_expiry_calls(), "CALL", 0.30, 0, 100, a)
    assert q.code == "C-NEAR"  # only the near one is inside a 0..100 DTE window
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `python3 -m pytest tests/test_options_chain.py -k pin_expiry -v`
Expected: FAIL — `select_contract() got an unexpected keyword argument 'pin_expiry'`.

- [ ] **Step 3: Add the `pin_expiry` parameter**

Replace `select_contract` in `autotrader/options/chain.py` with:

```python
def select_contract(quotes: List[OptionQuote], right: OptionRight,
                    target_delta: float, dte_min: int, dte_max: int,
                    asof: date, pin_expiry: Optional[date] = None) -> Optional[OptionQuote]:
    """Closest-to-target-|delta| contract of `right` whose DTE is in
    [dte_min, dte_max] and premium > 0. When `pin_expiry` is set, candidates are
    further restricted to that exact expiry (used to keep multi-leg single-expiry
    strategies on one expiry). None if nothing qualifies. Ties broken by nearest
    expiry, then lowest strike (deterministic regardless of input order)."""
    target = abs(target_delta)
    candidates = [
        q for q in quotes
        if q.right == right and q.premium > 0
        and dte_min <= (q.expiry - asof).days <= dte_max
        and (pin_expiry is None or q.expiry == pin_expiry)
    ]
    if not candidates:
        return None
    return min(candidates, key=lambda q: (abs(abs(q.delta) - target),
                                          (q.expiry - asof).days, q.strike))
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `python3 -m pytest tests/test_options_chain.py -k pin_expiry -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add autotrader/options/chain.py tests/test_options_chain.py
git commit -m "feat(options): select_contract pin_expiry for single-expiry multi-leg"
```

---

### Task 5: Config `option_default_contracts`

**Files:**
- Modify: `autotrader/config.py:38-48` (dataclass), `:101-108` (`load_risk_config`)
- Test: `tests/test_risk_core_options.py`

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_risk_core_options.py`:

```python
def test_option_default_contracts_defaults_to_one(monkeypatch):
    monkeypatch.setenv("RISK_ALLOWED_SYMBOLS", "US.AAPL")
    cfg = load_risk_config()
    assert cfg.option_default_contracts == 1


def test_option_default_contracts_parses_env(monkeypatch):
    monkeypatch.setenv("RISK_ALLOWED_SYMBOLS", "US.AAPL")
    monkeypatch.setenv("RISK_OPTION_DEFAULT_CONTRACTS", "3")
    cfg = load_risk_config()
    assert cfg.option_default_contracts == 3
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python3 -m pytest tests/test_risk_core_options.py -k option_default_contracts -v`
Expected: FAIL — `AttributeError: ... 'option_default_contracts'`.

- [ ] **Step 3: Add the field and parsing**

In `autotrader/config.py`, add to the options block of the `RiskConfig` dataclass (after `option_profit_target_pct`):

```python
    option_profit_target_pct: float = 0.5
    # Contract count for strategies that are NOT share-covered (spread / diagonal /
    # LEAP). Share-covered overlays (covered call, collar) still size off held shares.
    option_default_contracts: int = 1
```

In `load_risk_config()`, add to the `RiskConfig(...)` constructor call (after `option_profit_target_pct=...`):

```python
        option_profit_target_pct=_f("RISK_OPTION_PROFIT_TARGET_PCT", 0.5),
        option_default_contracts=int(_f("RISK_OPTION_DEFAULT_CONTRACTS", 1)),
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python3 -m pytest tests/test_risk_core_options.py -k option_default_contracts -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add autotrader/config.py tests/test_risk_core_options.py
git commit -m "feat(config): option_default_contracts for non-share-covered overlays"
```

---

### Task 6: Planner — per-leg selection, anchor/pin, sizing fork, structure validation

**Files:**
- Modify: `autotrader/options/planner.py:33-81`
- Test: `tests/test_options_planner.py`

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_options_planner.py` (after the existing imports/helpers). First add a richer chain and a multi-strategy config helper:

```python
def _far():
    return _asof() + timedelta(days=300)


def _rich_chains():
    a, near, far = _asof(), _asof() + timedelta(days=35), _far()
    return {
        ("US.AAPL", "CALL"): [
            OptionQuote("US.AAPL270412C180000", "US.AAPL", far, 180, "CALL", 0.80, 30.0),
            OptionQuote("US.AAPL270412C195000", "US.AAPL", far, 195, "CALL", 0.70, 20.0),
            OptionQuote("US.AAPL260721C210000", "US.AAPL", near, 210, "CALL", 0.30, 1.5),
        ],
        ("US.AAPL", "PUT"): [
            OptionQuote("US.AAPL260721P200000", "US.AAPL", near, 200, "PUT", -0.45, 4.0),
            OptionQuote("US.AAPL260721P190000", "US.AAPL", near, 190, "PUT", -0.30, 2.0),
            OptionQuote("US.AAPL260721P185000", "US.AAPL", near, 185, "PUT", -0.22, 1.5),
        ],
    }


def _rich_broker():
    return SimBroker(quotes={"US.AAPL": 200.0}, option_chains=_rich_chains())


def _cfgN(**over):
    # all four phantom overlays enabled; premium cap high enough for LEAP/PMCC long legs
    base = dict(allowed_overlays=frozenset({
        "COVERED_CALL", "PROTECTIVE_PUT", "COLLAR",
        "BEAR_PUT_SPREAD", "CALL_DIAGONAL", "LEAP"}),
        max_option_premium_per_trade=5000.0, option_default_contracts=1)
    base.update(over)
    return _cfg(**base)


def test_collar_plan_is_long_put_and_short_call_same_expiry():
    plan = build_overlay_plan(_sig(OverlayType.COLLAR), _snap(100),
                              _rich_broker(), _cfgN(), "s", _asof())
    assert isinstance(plan, OverlayPlan)
    by_side = {l.request.side: l for l in plan.legs}
    assert by_side["BUY"].request.option.right == "PUT"
    assert by_side["SELL"].request.option.right == "CALL"
    assert by_side["BUY"].request.option.expiry == by_side["SELL"].request.option.expiry
    assert by_side["BUY"].request.option.code == "US.AAPL260721P190000"  # 0.30 put
    assert by_side["SELL"].request.option.code == "US.AAPL260721C210000"  # 0.30 call


def test_bear_put_spread_plan_long_higher_short_lower_same_expiry():
    plan = build_overlay_plan(_sig(OverlayType.BEAR_PUT_SPREAD), _snap(0),
                              _rich_broker(), _cfgN(), "s", _asof())
    assert isinstance(plan, OverlayPlan)
    by_side = {l.request.side: l for l in plan.legs}
    assert by_side["BUY"].request.option.strike == 200   # 0.45 delta long
    assert by_side["SELL"].request.option.strike == 185  # 0.22 (closest to 0.25) short
    assert by_side["BUY"].request.qty == 1               # option_default_contracts
    assert by_side["BUY"].request.option.expiry == by_side["SELL"].request.option.expiry


def test_call_diagonal_plan_long_far_short_near_different_expiries():
    plan = build_overlay_plan(_sig(OverlayType.CALL_DIAGONAL), _snap(0),
                              _rich_broker(), _cfgN(), "s", _asof())
    assert isinstance(plan, OverlayPlan)
    by_side = {l.request.side: l for l in plan.legs}
    assert by_side["BUY"].request.option.strike == 180          # deep-ITM 0.80
    assert by_side["BUY"].request.option.expiry == _far()
    assert by_side["SELL"].request.option.strike == 210         # near 0.30
    assert by_side["BUY"].request.option.expiry > by_side["SELL"].request.option.expiry


def test_leap_plan_single_long_call_sized_by_default_contracts():
    plan = build_overlay_plan(_sig(OverlayType.LEAP), _snap(0),
                              _rich_broker(), _cfgN(option_default_contracts=2), "s", _asof())
    assert isinstance(plan, OverlayPlan)
    assert len(plan.legs) == 1
    leg = plan.legs[0].request
    assert leg.side == "BUY" and leg.option.right == "CALL"
    assert leg.option.code == "US.AAPL270412C195000" and leg.qty == 2


def test_non_covered_strategy_sized_zero_is_disabled():
    skip = build_overlay_plan(_sig(OverlayType.LEAP), _snap(0), _rich_broker(),
                              _cfgN(option_default_contracts=0), "s", _asof())
    assert isinstance(skip, OverlaySkip) and skip.reason == "SKIP_OVERLAY_DISABLED"


def test_bear_put_spread_invalid_structure_when_no_debit():
    # A chain where the two selected puts invert the debit (long premium < short).
    a, near = _asof(), _asof() + timedelta(days=35)
    bad = {("US.AAPL", "PUT"): [
        OptionQuote("LONG", "US.AAPL", near, 200, "PUT", -0.45, 1.0),   # long, cheap
        OptionQuote("SHORT", "US.AAPL", near, 185, "PUT", -0.25, 4.0),  # short, rich
    ]}
    b = SimBroker(quotes={"US.AAPL": 200.0}, option_chains=bad)
    skip = build_overlay_plan(_sig(OverlayType.BEAR_PUT_SPREAD), _snap(0), b,
                              _cfgN(), "s", _asof())
    assert isinstance(skip, OverlaySkip) and skip.reason == "SKIP_INVALID_STRUCTURE"


def test_collar_skips_no_contract_when_window_empty():
    skip = build_overlay_plan(_sig(OverlayType.COLLAR), _snap(100), _rich_broker(),
                              _cfgN(), "s", _asof() + timedelta(days=400))
    assert isinstance(skip, OverlaySkip) and skip.reason == "SKIP_NO_CONTRACT"
```

Also **update** the now-stale `test_skip_unsupported_overlay` (COLLAR is registered now) to delete an entry so the path stays covered:

```python
def test_skip_unsupported_overlay(monkeypatch):
    from autotrader.options import overlays
    monkeypatch.delitem(overlays.REGISTRY, OverlayType.COLLAR)
    skip = build_overlay_plan(_sig(OverlayType.COLLAR), _snap(100),
                              _broker(), _cfg(allowed_overlays=frozenset({"COLLAR"})),
                              "s", _asof())
    assert isinstance(skip, OverlaySkip) and skip.reason == "SKIP_UNSUPPORTED_OVERLAY"
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python3 -m pytest tests/test_options_planner.py -k "collar or bear_put or diagonal or leap or non_covered or invalid_structure or no_contract_when_window" -v`
Expected: FAIL — collar/spread/etc. either raise on unknown kwargs or build wrong/empty plans; `SKIP_INVALID_STRUCTURE` not produced.

- [ ] **Step 3: Rewrite the planner**

Replace the body of `autotrader/options/planner.py` from the `OverlaySkip` dataclass through the end with:

```python
@dataclass(frozen=True)
class OverlaySkip:
    overlay: OverlayType
    underlying: str
    reason: str   # SKIP_OVERLAY_DISABLED | SKIP_UNSUPPORTED_OVERLAY
                  # | SKIP_NO_UNDERLYING | SKIP_NO_CONTRACT | SKIP_INVALID_STRUCTURE


def _validate_structure(overlay: OverlayType, legs) -> Union[str, None]:
    """Defined-risk sanity check after contracts are selected. Returns a skip
    reason string or None. Defends the coverage guarantee the risk core relies on."""
    longs = [l for l in legs if l.request.side == "BUY"]
    shorts = [l for l in legs if l.request.side == "SELL"]
    if overlay is OverlayType.BEAR_PUT_SPREAD:
        lo, sh = longs[0].quote, shorts[0].quote
        if lo.expiry != sh.expiry:
            return "SKIP_INVALID_STRUCTURE"
        if not (lo.strike > sh.strike):
            return "SKIP_INVALID_STRUCTURE"
        if (lo.premium - sh.premium) <= 0:   # must be a net debit
            return "SKIP_INVALID_STRUCTURE"
    elif overlay is OverlayType.CALL_DIAGONAL:
        lo, sh = longs[0].quote, shorts[0].quote
        if not (lo.strike <= sh.strike):     # long is deeper ITM / not higher strike
            return "SKIP_INVALID_STRUCTURE"
        if not (lo.expiry > sh.expiry):      # long dated later than the short
            return "SKIP_INVALID_STRUCTURE"
    return None


def build_overlay_plan(signal: Signal, snapshot: AccountSnapshot, chain_provider,
                       cfg: RiskConfig, signal_id: str,
                       asof: date) -> Union[OverlayPlan, OverlaySkip]:
    overlay = signal.overlay
    if overlay is None:
        raise ValueError("build_overlay_plan requires signal.overlay to be set")
    underlying = signal.symbol.upper()

    if overlay.value not in cfg.allowed_overlays:
        return OverlaySkip(overlay, underlying, "SKIP_OVERLAY_DISABLED")
    if overlay not in REGISTRY:
        return OverlaySkip(overlay, underlying, "SKIP_UNSUPPORTED_OVERLAY")

    deff = REGISTRY[overlay]

    # Sizing fork: share-covered overlays size off held shares; non-covered
    # strategies (spread/diagonal/LEAP) size off the configured default count.
    if deff.requires_underlying:
        held = snapshot.position_qty(underlying)
        contracts = min(held // 100, cfg.max_option_contracts)
        if contracts < 1:
            return OverlaySkip(overlay, underlying, "SKIP_NO_UNDERLYING")
    else:
        contracts = min(cfg.option_default_contracts, cfg.max_option_contracts)
        if contracts < 1:
            return OverlaySkip(overlay, underlying, "SKIP_OVERLAY_DISABLED")

    corr = f"ov-{signal_id}-{overlay.value}"
    legs = []
    anchor_expiry = None
    for i, spec in enumerate(deff.legs):
        target_delta = spec.target_delta if spec.target_delta is not None else cfg.option_target_delta
        dte_min = spec.dte_min if spec.dte_min is not None else cfg.option_dte_min
        dte_max = spec.dte_max if spec.dte_max is not None else cfg.option_dte_max
        pin = anchor_expiry if (deff.single_expiry and i > 0) else None
        quotes = chain_provider.get_option_chain(underlying, spec.right)
        q = select_contract(quotes, spec.right, target_delta, dte_min, dte_max,
                            asof, pin_expiry=pin)
        if q is None:
            return OverlaySkip(overlay, underlying, "SKIP_NO_CONTRACT")
        if i == 0:
            anchor_expiry = q.expiry
        contract = to_contract(q)
        qty = contracts * spec.ratio
        cid = OrderRouter.make_client_order_id(
            q.code, spec.side, qty, f"{signal_id}-{overlay.value}-{i}")
        req = OrderRequest(symbol=q.code, side=spec.side, qty=qty,
                           order_type="MARKET", limit_price=None,
                           client_order_id=cid, option=contract,
                           position_effect=spec.position_effect,
                           correlation_id=corr)
        legs.append(OverlayLeg(req, q))

    bad = _validate_structure(overlay, legs)
    if bad is not None:
        return OverlaySkip(overlay, underlying, bad)

    exit_rule = ExitRule(dte_to_close=cfg.option_dte_to_close,
                         profit_target_pct=cfg.option_profit_target_pct)
    return OverlayPlan(overlay, underlying, tuple(legs), exit_rule, corr)
```

(The `OverlayLeg` and `OverlayPlan` dataclasses above `OverlaySkip` are unchanged — keep them.)

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python3 -m pytest tests/test_options_planner.py -v`
Expected: PASS (new + existing O1 planner tests, including the updated `test_skip_unsupported_overlay`).

- [ ] **Step 5: Commit**

```bash
git add autotrader/options/planner.py tests/test_options_planner.py
git commit -m "feat(options): planner per-leg selection, anchor/pin, sizing fork, structure check"
```

---

### Task 7: Risk core — defined-risk coverage via `coverage_legs`

**Files:**
- Modify: `autotrader/risk_core.py:9` (import), `:46-64` (`evaluate`), `:108-138` (`_evaluate_option_leg` + new helper)
- Test: `tests/test_risk_core_options.py`

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_risk_core_options.py` (it imports `OptionContract`, `OrderRequest`, `evaluate`):

```python
def _long_put(strike=200, expiry=date(2026, 7, 17), qty=1):
    c = OptionContract(underlying="US.AAPL", expiry=expiry, strike=strike,
                       right="PUT", code=f"US.AAPL260717P{int(strike)*1000:06d}")
    return OrderRequest(symbol=c.code, side="BUY", qty=qty, order_type="MARKET",
                        limit_price=None, client_order_id="long-p", option=c,
                        position_effect="OPEN", correlation_id="k")


def _short_put(strike=185, expiry=date(2026, 7, 17), qty=1):
    c = OptionContract(underlying="US.AAPL", expiry=expiry, strike=strike,
                       right="PUT", code=f"US.AAPL260717P{int(strike)*1000:06d}")
    return OrderRequest(symbol=c.code, side="SELL", qty=qty, order_type="MARKET",
                        limit_price=None, client_order_id="short-p", option=c,
                        position_effect="OPEN", correlation_id="k")


def _long_call(strike=180, expiry=date(2027, 4, 17), qty=1):
    c = OptionContract(underlying="US.AAPL", expiry=expiry, strike=strike,
                       right="CALL", code=f"US.AAPL270417C{int(strike)*1000:06d}")
    return OrderRequest(symbol=c.code, side="BUY", qty=qty, order_type="MARKET",
                        limit_price=None, client_order_id="long-c", option=c,
                        position_effect="OPEN", correlation_id="k")


def test_short_put_covered_by_long_put_approved():
    d = evaluate(_short_put(strike=185, qty=1), _snap(aapl_shares=0), _cfg(),
                 ref_price=1.5, coverage_legs=(_long_put(strike=200, qty=1),))
    assert d.approved, d.reason


def test_short_put_without_long_cover_rejected():
    d = evaluate(_short_put(strike=185, qty=1), _snap(aapl_shares=100), _cfg(),
                 ref_price=1.5)   # shares never cover a short put
    assert not d.approved and "uncovered" in d.reason.lower()


def test_short_call_covered_by_long_call_diagonal_approved():
    # PMCC: long 180-strike far call covers a near short 210 call.
    sc = OrderRequest(symbol="US.AAPL260717C210000", side="SELL", qty=1,
                      order_type="MARKET", limit_price=None, client_order_id="sc",
                      option=OptionContract(underlying="US.AAPL", expiry=date(2026, 7, 17),
                                            strike=210, right="CALL",
                                            code="US.AAPL260717C210000"),
                      position_effect="OPEN", correlation_id="k")
    d = evaluate(sc, _snap(aapl_shares=0), _cfg(), ref_price=1.5,
                 coverage_legs=(_long_call(strike=180, qty=1),))
    assert d.approved, d.reason


def test_long_call_cover_insufficient_when_strike_higher_than_short():
    # A long call with strike ABOVE the short does not cover it.
    sc = OrderRequest(symbol="US.AAPL260717C210000", side="SELL", qty=1,
                      order_type="MARKET", limit_price=None, client_order_id="sc",
                      option=OptionContract(underlying="US.AAPL", expiry=date(2026, 7, 17),
                                            strike=210, right="CALL",
                                            code="US.AAPL260717C210000"),
                      position_effect="OPEN", correlation_id="k")
    d = evaluate(sc, _snap(aapl_shares=0), _cfg(), ref_price=1.5,
                 coverage_legs=(_long_call(strike=220, qty=1),))
    assert not d.approved and "uncovered" in d.reason.lower()


def test_collar_short_call_still_share_covered():
    # No coverage_legs needed: 100 shares cover the short call (collar).
    d = evaluate(_call(qty=1), _snap(aapl_shares=100), _cfg(), ref_price=1.5)
    assert d.approved, d.reason
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python3 -m pytest tests/test_risk_core_options.py -k "covered_by_long or without_long_cover or diagonal_approved or insufficient_when_strike or collar_short" -v`
Expected: FAIL — `evaluate()` has no `coverage_legs` kwarg; short puts rejected even when covered.

- [ ] **Step 3: Add coverage logic**

In `autotrader/risk_core.py`, update the import line:

```python
from typing import Optional, Tuple
```

Add this helper near `_short_option_contracts`:

```python
def _long_cover_contracts(coverage_legs, opt) -> int:
    """Contracts of long OPEN option legs (in the same plan) that bound the risk
    of a short leg of `opt`'s right on the same underlying (defined-risk coverage):
      short CALL  <- long CALL with strike <= short strike AND expiry >= short expiry
      short PUT   <- long PUT  with strike >= short strike AND expiry >= short expiry
    """
    total = 0
    for c in coverage_legs:
        co = c.option
        if co is None or c.side != "BUY" or c.position_effect != "OPEN":
            continue
        if co.underlying.upper() != opt.underlying.upper() or co.right != opt.right:
            continue
        if opt.right == "CALL":
            ok = co.strike <= opt.strike and co.expiry >= opt.expiry
        else:  # PUT
            ok = co.strike >= opt.strike and co.expiry >= opt.expiry
        if ok:
            total += c.qty
    return total
```

Change the `evaluate` signature and the option dispatch:

```python
def evaluate(req: OrderRequest, snapshot: AccountSnapshot, cfg: RiskConfig,
             ref_price: Optional[float],
             coverage_legs: Tuple[OrderRequest, ...] = ()) -> RiskDecision:
```

and the dispatch line inside `evaluate`:

```python
    if req.option is not None:
        return _evaluate_option_leg(req, snapshot, cfg, ref_price, coverage_legs)
```

Change `_evaluate_option_leg`'s signature and replace its short-leg coverage block. New signature:

```python
def _evaluate_option_leg(req: OrderRequest, snapshot: AccountSnapshot,
                         cfg: RiskConfig, ref_price: Optional[float],
                         coverage_legs: Tuple[OrderRequest, ...] = ()) -> RiskDecision:
```

Replace the existing `if req.side == "SELL" and req.position_effect == "OPEN":` block (the share-only guard) with:

```python
    if req.side == "SELL" and req.position_effect == "OPEN":
        need = req.qty  # contracts
        existing_short = _short_option_contracts(snapshot, underlying, opt.right)
        # Shares only cover short CALLs (covered call / collar). Short PUTs are
        # never share-covered — only a long put bounds them.
        share_cover = (snapshot.position_qty(underlying) // opt.multiplier
                       if opt.right == "CALL" else 0)
        long_cover = _long_cover_contracts(coverage_legs, opt)
        available = share_cover + long_cover - existing_short
        if available < need:
            return RiskDecision(
                False,
                f"uncovered short: covered {available} < required {need} contract(s) "
                f"(shares cover {share_cover}, long-leg cover {long_cover}, "
                f"{existing_short} short {opt.right} contract(s) open)")
    else:
```

(The `else:` debit-premium branch and the daily-loss block after it are unchanged.)

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python3 -m pytest tests/test_risk_core_options.py -v`
Expected: PASS (new + all existing O1 risk tests — coverage math is contract-equivalent for the share-covered cases).

- [ ] **Step 5: Commit**

```bash
git add autotrader/risk_core.py tests/test_risk_core_options.py
git commit -m "feat(risk): defined-risk coverage for multi-leg option shorts"
```

---

### Task 8: Engine — coverage wiring + loud long-only residual

**Files:**
- Modify: `autotrader/main.py:186-205` (the leg-submission loop in `_route_overlay`)
- Test: `tests/test_options_e2e.py`

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_options_e2e.py`. First extend the fixtures to a multi-strike/expiry chain and an all-overlays config:

```python
def _rich_chains():
    near, far = ASOF + timedelta(days=35), ASOF + timedelta(days=300)
    return {
        ("US.AAPL", "CALL"): [
            OptionQuote("US.AAPL270412C180000", "US.AAPL", far, 180, "CALL", 0.80, 30.0),
            OptionQuote("US.AAPL270412C195000", "US.AAPL", far, 195, "CALL", 0.70, 20.0),
            OptionQuote("US.AAPL260721C210000", "US.AAPL", near, 210, "CALL", 0.30, 1.5),
        ],
        ("US.AAPL", "PUT"): [
            OptionQuote("US.AAPL260721P200000", "US.AAPL", near, 200, "PUT", -0.45, 4.0),
            OptionQuote("US.AAPL260721P190000", "US.AAPL", near, 190, "PUT", -0.30, 2.0),
            OptionQuote("US.AAPL260721P185000", "US.AAPL", near, 185, "PUT", -0.22, 1.5),
        ],
    }


def _rich_broker(shares=0):
    b = SimBroker(quotes={
        "US.AAPL": 200.0,
        "US.AAPL270412C180000": 30.0, "US.AAPL270412C195000": 20.0,
        "US.AAPL260721C210000": 1.5,
        "US.AAPL260721P200000": 4.0, "US.AAPL260721P190000": 2.0,
        "US.AAPL260721P185000": 1.5,
    }, option_chains=_rich_chains())
    if shares:
        from autotrader.domain import OrderRequest
        b.place_order(OrderRequest(symbol="US.AAPL", side="BUY", qty=shares,
                                   order_type="MARKET", limit_price=None,
                                   client_order_id="seed"))
    return b


def _cfgN(**over):
    return _cfg(allowed_overlays=frozenset({
        "COVERED_CALL", "PROTECTIVE_PUT", "COLLAR",
        "BEAR_PUT_SPREAD", "CALL_DIAGONAL", "LEAP"}),
        max_option_premium_per_trade=5000.0, option_default_contracts=1, **over)


def test_bear_put_spread_places_both_legs(tmp_path):
    b = _rich_broker(shares=0)
    eng = _engine(b, _cfgN(), tmp_path)
    res = eng.submit_external_signal(
        Signal("US.AAPL", "SELL", 0.7, "bear put", overlay=OverlayType.BEAR_PUT_SPREAD))
    assert res.action == "OVERLAY_PLACED", res
    held = {p.symbol: p.qty for p in b.get_account().positions}
    assert held["US.AAPL260721P200000"] == 1    # long higher-strike put
    assert held["US.AAPL260721P185000"] == -1   # short lower-strike put


def test_call_diagonal_places_both_legs(tmp_path):
    b = _rich_broker(shares=0)
    eng = _engine(b, _cfgN(), tmp_path)
    res = eng.submit_external_signal(
        Signal("US.AAPL", "SELL", 0.7, "pmcc", overlay=OverlayType.CALL_DIAGONAL))
    assert res.action == "OVERLAY_PLACED", res
    held = {p.symbol: p.qty for p in b.get_account().positions}
    assert held["US.AAPL270412C180000"] == 1    # long LEAP call
    assert held["US.AAPL260721C210000"] == -1   # short near call


def test_leap_places_single_long_call(tmp_path):
    b = _rich_broker(shares=0)
    eng = _engine(b, _cfgN(), tmp_path)
    res = eng.submit_external_signal(
        Signal("US.AAPL", "SELL", 0.7, "leap", overlay=OverlayType.LEAP))
    assert res.action == "OVERLAY_PLACED", res
    held = {p.symbol: p.qty for p in b.get_account().positions}
    assert held["US.AAPL270412C195000"] == 1


def test_collar_places_long_put_and_short_call(tmp_path):
    b = _rich_broker(shares=100)
    eng = _engine(b, _cfgN(), tmp_path)
    res = eng.submit_external_signal(
        Signal("US.AAPL", "SELL", 0.7, "collar", overlay=OverlayType.COLLAR))
    assert res.action == "OVERLAY_PLACED", res
    held = {p.symbol: p.qty for p in b.get_account().positions}
    assert held["US.AAPL260721P190000"] == 1    # long put
    assert held["US.AAPL260721C210000"] == -1   # short call (share-covered)


class _RejectShortBroker(SimBroker):
    """Fills the long (BUY) leg, but REJECTS any short OPEN option leg — to
    exercise the long-only safe residual path."""
    def place_order(self, req):
        if req.option is not None and req.side == "SELL" and req.position_effect == "OPEN":
            self._seq += 1
            from autotrader.domain import OrderAck, OrderState
            return OrderAck(req.client_order_id, f"sim-{self._seq}", OrderState.REJECTED, {})
        return super().place_order(req)


def test_spread_short_leg_rejected_leaves_loud_long_residual(tmp_path):
    b = _RejectShortBroker(quotes={
        "US.AAPL": 200.0, "US.AAPL260721P200000": 4.0, "US.AAPL260721P185000": 1.5,
    }, option_chains=_rich_chains())
    eng = _engine(b, _cfgN(), tmp_path)
    res = eng.submit_external_signal(
        Signal("US.AAPL", "SELL", 0.7, "bear put", overlay=OverlayType.BEAR_PUT_SPREAD))
    assert res.action == "OVERLAY_RESIDUAL_LONG", res
    held = {p.symbol: p.qty for p in b.get_account().positions}
    assert held["US.AAPL260721P200000"] == 1            # long put filled
    assert "US.AAPL260721P185000" not in held           # short put never opened
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python3 -m pytest tests/test_options_e2e.py -k "bear_put or diagonal or leap or collar_places or residual" -v`
Expected: FAIL — short legs rejected as uncovered (no coverage context passed) and no `OVERLAY_RESIDUAL_LONG` action.

- [ ] **Step 3: Wire coverage + residual into `_route_overlay`**

In `autotrader/main.py`, replace the submission loop (currently `last_boid = None` through the final `return TickResult("OVERLAY_PLACED", ...)`) with:

```python
        # Defined-risk coverage context: the plan's long OPEN legs cover its short
        # legs (the risk core recognizes this). Long legs are submitted first, so a
        # short is only ever placed after its cover is acked.
        coverage = tuple(l.request for l in plan.legs
                         if l.request.side == "BUY" and l.request.position_effect == "OPEN")

        last_boid = None
        filled_long = []  # symbols of long legs already filled this overlay
        for leg in sorted(plan.legs, key=lambda l: 0 if l.request.side == "BUY" else 1):
            req = leg.request
            decision = evaluate(req, snap, self._cfg, ref_price=leg.quote.premium,
                                coverage_legs=coverage)
            if not decision.approved:
                logger.warning("overlay leg rejected (%s): %s", req.symbol, decision.reason)
                if filled_long:
                    logger.warning("overlay %s left long-only residual %s after reject: %s",
                                   plan.correlation_id, filled_long, decision.reason)
                    return TickResult("OVERLAY_RESIDUAL_LONG",
                                      f"{plan.correlation_id}:{','.join(filled_long)}")
                return TickResult("REJECTED_BY_RISK", decision.reason)
            ack = self._router.submit(req)
            if self._db:
                self._db.record_trade(
                    client_order_id=ack.client_order_id, symbol=req.symbol,
                    side=req.side, qty=req.qty, order_type=req.order_type,
                    limit_price=leg.quote.premium, broker_order_id=ack.broker_order_id,
                    state=ack.state.value)
            if ack.state in (OrderState.UNKNOWN, OrderState.REJECTED):
                if filled_long:
                    logger.warning("overlay %s left long-only residual %s after %s",
                                   plan.correlation_id, filled_long, ack.state.value)
                    return TickResult("OVERLAY_RESIDUAL_LONG",
                                      f"{plan.correlation_id}:{','.join(filled_long)}")
                action = "ORDER_UNKNOWN" if ack.state is OrderState.UNKNOWN else "ORDER_REJECTED"
                return TickResult(action, ack.client_order_id)
            if (req.side == "BUY" and req.position_effect == "OPEN"
                    and ack.state is OrderState.FILLED):
                filled_long.append(req.symbol)
            last_boid = ack.broker_order_id
        return TickResult("OVERLAY_PLACED", f"{plan.correlation_id}:{last_boid}")
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python3 -m pytest tests/test_options_e2e.py -v`
Expected: PASS (new + existing O1 e2e tests).

- [ ] **Step 5: Commit**

```bash
git add autotrader/main.py tests/test_options_e2e.py
git commit -m "feat(engine): coverage context + loud long-only residual for multi-leg overlays"
```

---

### Task 9: Docs — runbook + config example

**Files:**
- Modify: `RUNBOOK.md` (options overlay section)
- Modify: `config/risk.config.example` if it exists (else skip — verify in Step 1)

- [ ] **Step 1: Check what config example exists**

Run: `ls config/ && grep -rn "RISK_ALLOWED_OVERLAYS\|RISK_MAX_OPTION" RUNBOOK.md config/ 2>/dev/null`
Expected: shows the existing options env-var documentation block to extend.

- [ ] **Step 2: Document the new strategies and knob**

In `RUNBOOK.md`, in the options-overlay section, add the four strategies to the `RISK_ALLOWED_OVERLAYS` list and document the new env var and the premium-cap caveat:

```markdown
- `RISK_ALLOWED_OVERLAYS` accepts (comma-separated, case-insensitive):
  `COVERED_CALL, PROTECTIVE_PUT, COLLAR, BEAR_PUT_SPREAD, CALL_DIAGONAL, LEAP`.
- `RISK_OPTION_DEFAULT_CONTRACTS` (default 1): contract count for strategies that
  are NOT share-covered — `BEAR_PUT_SPREAD`, `CALL_DIAGONAL`, `LEAP`. Covered call
  and collar still size off held shares (`min(shares//100, max_option_contracts)`).
- Per-leg delta/DTE targets are structural (declared in `options/overlays.py`), not
  env vars. The global `RISK_OPTION_TARGET_DELTA / _DTE_MIN / _DTE_MAX` remain the
  fallback for legs that declare no override (covered call, protective put).
- CAVEAT: a deep-ITM LEAP / PMCC long call can cost far more than the default
  `RISK_MAX_OPTION_PREMIUM_PER_TRADE`. Raise that human-reviewed cap before enabling
  `LEAP` or `CALL_DIAGONAL`, or the long leg is rejected on premium.
- Partial fill: legs submit long-first; if a later leg fails after the long fills,
  the engine returns `OVERLAY_RESIDUAL_LONG` and leaves the long-only (risk-defined)
  residual in place — it is never a naked short. No automatic unwind (by design).
```

If `config/risk.config.example` exists and lists option vars, add `RISK_OPTION_DEFAULT_CONTRACTS=1` there with a comment mirroring the above.

- [ ] **Step 3: Commit**

```bash
git add RUNBOOK.md config/
git commit -m "docs(options): document phantom strategies, default-contracts knob, premium caveat"
```

---

### Final verification

- [ ] **Run the full offline suite**

Run: `python3 -m pytest -q`
Expected: all tests pass, 0 failures. The baseline was green; this plan adds ~25 tests and modifies two pre-existing tests (`test_unsupported_overlays_absent_from_registry` → `test_phantom_strategies_now_registered`; `test_skip_unsupported_overlay` → monkeypatched).

- [ ] **Confirm no naked-short regression**

Run: `python3 -m pytest tests/test_risk_core_options.py -v`
Expected: all coverage tests pass, including the uncovered-short rejections — the invariant holds.
