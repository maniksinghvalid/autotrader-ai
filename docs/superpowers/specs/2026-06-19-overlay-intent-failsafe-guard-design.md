# Overlay-Intent Fail-Safe Guard — Design

**Date:** 2026-06-19
**Branch:** `feat/autotrader-paper-v1`
**Status:** Approved (design); pending implementation plan
**Component:** `autotrader/signals/normalize.py`

## Problem

On 2026-06-19 the trader **liquidated three hedged positions it was supposed to
protect**: `SELL 200 US.CLOV`, `SELL 100 US.DIVO`, `SELL 100 US.SPCE` (13:20–13:21).

Root cause (confirmed): webhook `options-20260619-1307-20bec2` (ts 2026-06-19T13:20:01)
carried 12 `signal_changes`, **every one missing the `overlay` enum** while its
`driver` named an overlay (e.g. `"Collar (options overlay)"`, transition
`["LONG","HEDGE"]`). The chain:

1. `normalize_change` maps `overlay = OverlayType(change.overlay) if change.overlay
   else None` → **`overlay=None`**, and `direction "DOWN" → "SELL"`.
2. `_route_signal` sees `overlay is None` → skips the overlay path → plain SELL
   branch → [`main.py:112-113`](../../../autotrader/main.py) liquidates the **full
   position**.
3. A Collar (keep shares + sell call + buy put) became a full share sell.

The producer's later webhook (14:51) re-sent the same DIVO `DOWN` **with**
`"overlay": "COLLAR"`, confirming the field is required and the 13:07 routine
violated the contract. This is a recurrence of the 06-18 "missing overlay field"
class. The fix chosen is **local-only**: the trader must never convert an
overlay-intended instruction into a plain buy/sell, regardless of upstream defects.

(A separate, independent bug erased the audit trail that made this hard to trace —
`signal_id` is a per-process counter reset on restart and `record_signal` uses
`INSERT OR IGNORE`, so today's signal rows were silently dropped. **Out of scope
here — tracked as a separate spec.**)

## Decision

Add a **fail-safe guard in `normalize_payload`** (the single chokepoint every
external signal passes through before `submit_external_signal`): a change that is
*overlay-intended* but carries no `overlay` enum is **dropped** (no `Signal`
emitted) with a loud warning — never routed as a plain order.

This narrows the existing "`overlay` absent ⇒ plain equity" contract: absent
`overlay` means plain equity **only when there are no overlay-intent markers**.
With markers present, an absent `overlay` is a malformed instruction, not a
liquidation order.

| Aspect | Choice | Rationale |
|---|---|---|
| Placement | `normalize.py` (`normalize_payload`) | Pure/SDK-free; single chokepoint before any `Signal` exists; the bad mapping originates here |
| Detection | `driver` contains `"overlay"` (case-insensitive) **OR** `transition` target ∈ `{HEDGE, INCOME, BULLISH}` | Two independent cues; a wording change in one still trips the other |
| Action | Skip the change, `logger.warning` with ticker/direction/transition/driver | Fail safe: dropping is always safe; guessing the overlay is not |
| Scope | Both directions | DOWN → catastrophic liquidation; UP → wrongly buys stock instead of the intended overlay |
| Producer fix | Not included | User chose local-only |

## Component

### `autotrader/signals/normalize.py`

Add a pure predicate and apply it in `normalize_payload`:

```python
_OVERLAY_TRANSITIONS = {"HEDGE", "INCOME", "BULLISH"}


def _overlay_intended(change: SignalChange) -> bool:
    """True if the change signals an options-overlay instruction (by driver marker
    or transition target). Such a change MUST carry an `overlay` enum; if it does
    not, it is malformed and must not be routed as a plain equity order."""
    if "overlay" in (change.driver or "").lower():
        return True
    return bool(change.transition) and change.transition[-1].upper() in _OVERLAY_TRANSITIONS
```

In `normalize_payload`, filter before normalizing:

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

Behavior preserved for every legitimate case:
- A well-formed overlay change (`overlay` set) → normalized to an overlay `Signal` (unchanged).
- A genuine plain equity change (no `"overlay"` in driver, transition target not in
  the overlay set — e.g. `["HOLD","BUY"]`, `["LONG","AVOID"]`) → normalized as today,
  including real SELL exits.
- An empty `transition` is handled safely (`bool(change.transition)` short-circuits
  before indexing).

## Data Flow

```
SignalInbox.poll() → normalize_payload(payload, scale)
    per change:
        overlay-intended AND no overlay enum?  → log + drop (no Signal)
        else                                   → normalize_change(...) → Signal
  → runner.run_once → engine.submit_external_signal(signal) → _route_signal → router
```
The malformed change never becomes a `Signal`, so it can never reach the
SELL-full-position branch in `main.py`.

## Error Handling
- The guard is pure and total: it only reads `driver`/`transition`/`overlay`
  (all schema-validated), indexes `transition` only after a non-empty check, and
  logs+skips rather than raising. A dropped change does not reject the whole file
  (the inbox still moves it to `processed/`); other valid changes in the same
  payload normalize normally.

## Testing
- DOWN/HEDGE change, `overlay` absent → **dropped** (`normalize_payload` returns no
  `Signal` for it); a warning is logged.
- Same change with `overlay="COLLAR"` → produces an overlay `Signal`.
- UP/INCOME change, `overlay` absent → dropped.
- Driver marker alone trips it: transition `["LONG","SOMETHING"]` but
  `driver="Collar (options overlay)"`, no `overlay` → dropped.
- Transition marker alone trips it: `driver=""` but transition `["LONG","HEDGE"]`,
  no `overlay` → dropped.
- Genuine plain change: `driver=""`, transition `["HOLD","BUY"]`, no `overlay` →
  **normalizes** to a BUY `Signal` (not dropped).
- Genuine plain SELL exit: `driver=""`, transition `["LONG","AVOID"]` (target not in
  the overlay set), direction DOWN → normalizes to a SELL `Signal` (real exits still work).
- Empty `transition` + `driver=""` + no overlay → not overlay-intended → normalizes.
- Replay of the 13:20 payload (DIVO/CLOV/SPCE DOWN/HEDGE, overlay missing) →
  all overlay-intended legs dropped; no SELL `Signal` emitted.

## Out of Scope
- Producer-side fix (emit the `overlay` enum) — user chose local-only.
- Inferring/auto-applying the overlay from the `driver` text — fail safe by dropping.
- The `signal_id` reset + `INSERT OR IGNORE` audit-loss bug — separate spec.
- Hard-stop execution / catalyst logic — unrelated, still deferred.
```
