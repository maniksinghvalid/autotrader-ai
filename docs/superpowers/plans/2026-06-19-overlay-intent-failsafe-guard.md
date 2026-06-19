# Overlay-Intent Fail-Safe Guard Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Drop an overlay-intended `signal_change` that arrives without its `overlay` enum instead of routing it as a plain buy/sell, so a missing field can never liquidate a hedged position.

**Architecture:** A single pure predicate + a filter in `normalize_payload` (`autotrader/signals/normalize.py`), the one chokepoint every external signal passes through before `submit_external_signal`. Overlay-intent is detected by the `driver` string or the `transition` target; an overlay-intended change with no `overlay` enum is logged and skipped (no `Signal` produced).

**Tech Stack:** Python 3, pytest, pydantic v2 schema (`SignalChange`), stdlib `logging`.

## Global Constraints

- `normalize.py` stays pure and SDK-free (imports domain + schema only; no broker/SDK).
- The guard is total: it must not raise. It reads only schema-validated fields and indexes `transition` only after a non-empty check; on a match it logs + skips.
- Detection (overlay-intended) = `driver` contains `"overlay"` (case-insensitive) **OR** `transition` target (last element) ∈ `{HEDGE, INCOME, BULLISH}` (compared upper-cased).
- Action on `overlay-intended AND not change.overlay`: emit no `Signal`; `logger.warning` with ticker, direction, transition, driver. Applies to BOTH directions.
- Preserve all existing behavior: well-formed overlay changes still normalize; genuine plain-equity changes (incl. real SELL exits) still normalize. A dropped change must not reject the whole payload — other changes in the same payload still normalize.
- TDD: failing test first, watch it fail, minimal implementation, watch it pass, full suite, commit.
- Run the suite with: `PYTHONPATH=. python3 -m pytest tests/ -q`

---

### Task 1: Fail-safe guard in `normalize_payload`

**Files:**
- Modify: `autotrader/signals/normalize.py` (add `_overlay_intended`; rewrite the `return` of `normalize_payload` as a guarded loop)
- Test: `tests/test_signal_normalize.py` (add cases)

**Interfaces:**
- Consumes: `SignalChange` (already imported in `normalize.py`), `normalize_change` (existing), `_normalize_symbol` (existing), `logger` (existing).
- Produces:
  - `_overlay_intended(change: SignalChange) -> bool` — module-level pure predicate.
  - `normalize_payload(payload, confidence_scale=10.0) -> List[Signal]` — unchanged signature; now omits any change where `_overlay_intended(change) and not change.overlay`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_signal_normalize.py`:

```python
def test_drops_overlay_intended_change_missing_overlay_enum():
    # driver names an overlay + HEDGE transition, but no overlay enum -> dropped.
    payload = RoutineSignalPayload(
        routine_id="r", timestamp="2026-06-19T13:20:01",
        signal_changes=[_change(ticker="US.DIVO", direction="DOWN",
                                transition=["LONG", "HEDGE"], points_delta=-6,
                                driver="Collar (options overlay)")])
    assert normalize_payload(payload) == []


def test_keeps_overlay_change_when_enum_present():
    payload = RoutineSignalPayload(
        routine_id="r", timestamp="2026-06-19T13:20:01",
        signal_changes=[_change(ticker="US.DIVO", direction="DOWN",
                                transition=["LONG", "HEDGE"], points_delta=-6,
                                driver="Collar (options overlay)", overlay="COLLAR")])
    sigs = normalize_payload(payload)
    assert len(sigs) == 1
    assert sigs[0].symbol == "US.DIVO" and sigs[0].overlay is not None


def test_transition_marker_alone_trips_guard():
    # driver has no "overlay" marker, but transition target HEDGE does.
    payload = RoutineSignalPayload(
        routine_id="r", timestamp="2026-06-19T13:20:01",
        signal_changes=[_change(ticker="US.SPCE", direction="DOWN",
                                transition=["LONG", "HEDGE"], points_delta=-6,
                                driver="")])
    assert normalize_payload(payload) == []


def test_driver_marker_alone_trips_guard():
    # transition target is not an overlay state, but driver names an overlay.
    payload = RoutineSignalPayload(
        routine_id="r", timestamp="2026-06-19T13:20:01",
        signal_changes=[_change(ticker="US.O", direction="UP",
                                transition=["LONG", "SOMETHING"], points_delta=3,
                                driver="Covered Call (options overlay)")])
    assert normalize_payload(payload) == []


def test_up_income_overlay_intent_missing_enum_is_dropped():
    payload = RoutineSignalPayload(
        routine_id="r", timestamp="2026-06-19T13:20:01",
        signal_changes=[_change(ticker="US.IBIT", direction="UP",
                                transition=["LONG", "INCOME"], points_delta=3,
                                driver="Poor Man's Covered Call (options overlay)")])
    assert normalize_payload(payload) == []


def test_genuine_plain_buy_is_not_dropped():
    payload = RoutineSignalPayload(
        routine_id="r", timestamp="2026-06-19T13:20:01",
        signal_changes=[_change(ticker="US.DIVO", direction="UP",
                                transition=["HOLD", "BUY"], points_delta=7, driver="")])
    sigs = normalize_payload(payload)
    assert len(sigs) == 1 and sigs[0].direction == "BUY" and sigs[0].overlay is None


def test_genuine_plain_sell_exit_is_not_dropped():
    # A real exit (transition target not an overlay state, no overlay driver) still sells.
    payload = RoutineSignalPayload(
        routine_id="r", timestamp="2026-06-19T13:20:01",
        signal_changes=[_change(ticker="US.YNVDA", direction="DOWN",
                                transition=["CAUTION", "AVOID"], points_delta=-17,
                                driver="ticker sweep")])
    sigs = normalize_payload(payload)
    assert len(sigs) == 1 and sigs[0].direction == "SELL"


def test_empty_transition_no_driver_is_not_overlay_intended():
    payload = RoutineSignalPayload(
        routine_id="r", timestamp="2026-06-19T13:20:01",
        signal_changes=[_change(ticker="US.AAPL", direction="UP",
                                transition=[], points_delta=5, driver="")])
    assert len(normalize_payload(payload)) == 1


def test_mixed_payload_drops_only_malformed_overlay_change():
    payload = RoutineSignalPayload(
        routine_id="r", timestamp="2026-06-19T13:20:01",
        signal_changes=[
            _change(ticker="US.DIVO", direction="DOWN", transition=["LONG", "HEDGE"],
                    points_delta=-6, driver="Collar (options overlay)"),        # dropped
            _change(ticker="US.AAPL", direction="UP", transition=["HOLD", "BUY"],
                    points_delta=7, driver=""),                                  # kept
        ])
    sigs = normalize_payload(payload)
    assert [s.symbol for s in sigs] == ["US.AAPL"]
```

- [ ] **Step 2: Run the new tests to verify they fail**

Run: `PYTHONPATH=. python3 -m pytest tests/test_signal_normalize.py -q`
Expected: the new `test_drops_*` / `test_*_trips_guard` / `test_up_income_*` / `test_mixed_*` tests FAIL (the current `normalize_payload` produces a `Signal` for every change, so the drop assertions fail). The `test_genuine_*` / `test_empty_*` / `test_keeps_*` tests pass already (they assert preserved behavior).

- [ ] **Step 3: Implement the predicate + guard**

In `autotrader/signals/normalize.py`, add the predicate after the `_DIRECTION` constant (after line 24):

```python
_OVERLAY_TRANSITIONS = {"HEDGE", "INCOME", "BULLISH"}


def _overlay_intended(change: SignalChange) -> bool:
    """True if the change signals an options-overlay instruction — by the driver
    marker the producer appends ("... (options overlay)") or by a transition whose
    target is an overlay state. Such a change MUST carry an `overlay` enum; if it
    does not, it is malformed and must not be routed as a plain equity order."""
    if "overlay" in (change.driver or "").lower():
        return True
    return bool(change.transition) and change.transition[-1].upper() in _OVERLAY_TRANSITIONS
```

Replace the `return [...]` at the end of `normalize_payload` (the current lines 62-64) with a guarded loop:

```python
    signals = []
    for c in payload.signal_changes:
        if _overlay_intended(c) and not c.overlay:
            logger.warning(
                "DROPPED overlay-intended signal with no overlay enum: %s %s "
                "transition=%s driver=%r — refusing to route as a plain order",
                c.ticker, c.direction, c.transition, c.driver)
            continue
        signals.append(normalize_change(c, confidence_scale,
                                        stops.get(_normalize_symbol(c.ticker))))
    return signals
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `PYTHONPATH=. python3 -m pytest tests/test_signal_normalize.py -q`
Expected: PASS (all, including the pre-existing normalize tests).

- [ ] **Step 5: Run the full suite (no regressions)**

Run: `PYTHONPATH=. python3 -m pytest tests/ -q`
Expected: all pass. The existing `_change` default (`transition=["50","200"]`, `driver="breakout"`) is not overlay-intended, so other suites are unaffected.

- [ ] **Step 6: Commit**

```bash
git add autotrader/signals/normalize.py tests/test_signal_normalize.py
git commit -m "feat(signals): fail-safe drop overlay-intended signals missing the overlay enum

A signal_change whose driver names an overlay or whose transition target is
HEDGE/INCOME/BULLISH but carries no overlay enum is now dropped (logged), not
routed as a plain order. Prevents missing-overlay DOWN/HEDGE signals from
liquidating hedged positions (2026-06-19 CLOV/DIVO/SPCE incident).

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Self-Review

- **Spec coverage:** placement in `normalize_payload` (Task 1 Step 3) ✓ · detection by driver-marker OR transition-target ∈ {HEDGE,INCOME,BULLISH} (`_overlay_intended`) ✓ · action skip + `logger.warning`, both directions (guard loop) ✓ · pure/total, empty-transition safe (`bool(change.transition)` before indexing) ✓ · preserves well-formed overlay, genuine plain buy, genuine SELL exit, and per-change drop within a mixed payload (dedicated tests) ✓ · replay-of-13:20 behavior covered by the DROP + mixed tests ✓. All spec points mapped.
- **Placeholder scan:** none — every step has concrete code/tests/commands.
- **Type consistency:** `_overlay_intended(change: SignalChange) -> bool` matches its call in the loop; `normalize_payload` signature unchanged; `_normalize_symbol`, `normalize_change`, `logger`, `stops` all pre-exist in the function/module; `overlay="COLLAR"` is a valid `SignalChange.overlay` Literal.
```
