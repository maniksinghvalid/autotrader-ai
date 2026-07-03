# Covered-Share Reservation on Rebalance Trims — Design

**Date:** 2026-06-22
**Status:** Approved (design); implementation pending
**Repo of change:** `AutoTrader`
**Related:** [`2026-06-16-portfolio-rebalancing-design.md`](2026-06-16-portfolio-rebalancing-design.md) (the rebalancer this hardens); [`2026-06-22-portfolio-targets-emission-design.md`](2026-06-22-portfolio-targets-emission-design.md) §9 (which deferred this gap).

## 1. Problem

The midday rebalancer trims overweight equity positions with partial SELLs. But
some held shares may be **pledged as cover to an open short call** (covered call,
collar). The risk core enforces share-coverage only when an option is *written*
(`_evaluate_option_leg`: `share_cover = position_qty(underlying)//multiplier` vs
`existing_short`) — never when the underlying is *reduced*. The equity-SELL path
(`risk_core._evaluate`) checks only long-only / notional / exposure.

Concretely: a rebalance trim of a position from 100 → 60 shares while an open
short call needs 100 shares of cover leaves that call **naked by 40 shares**. The
same gap exists on other SELL paths (strategy stop-loss exits, external signals),
but those are intentional exits handled separately; this spec closes the gap on
the path the rebalancing feature introduced.

This gap is the hard precondition recorded in the portfolio-targets spec §9:
it must be closed before `RISK_REBALANCE_ENABLED` is set true on any book holding
option overlays.

## 2. Scope decision

**Enforce in the rebalancer (`compute_plan`) only** — not in `risk_core`.

- Targets exactly the path this rebalancing effort introduced.
- Lowest blast radius: emergency flatten (loss-halt liquidation) and strategy
  stop-loss exits — both intentional full liquidations routed through the same
  `evaluate()` chokepoint — remain untouched. A blanket `risk_core` guard would
  also constrain those exits, which is undesirable (an exit you want honored
  should not be blocked to preserve option coverage).
- The pre-existing strategy-exit/external-SELL coverage gap is **out of scope**;
  it is not introduced or worsened by rebalancing.

## 3. Key decisions

| # | Decision | Choice |
|---|----------|--------|
| C1 | Enforcement layer | **Rebalancer `compute_plan` only.** No change to `risk_core` evaluation logic or any non-rebalance SELL path. |
| C2 | What pledges shares | **Open short CALLs only.** Floor = `short_call_contracts × 100`. Long puts (protective) pledge nothing; v1 is long-only equity, so there are no share-pledging short puts. |
| C3 | Trim-into-floor behavior | **Partial trim to the covered floor.** Sell only the freely-sellable shares (`current_qty − covered_floor`); rebalance as far as safely possible; the position may remain above target, pinned at the floor. |
| C4 | Fully/over pledged | If free-to-sell `≤ 0`, skip the symbol with reason **`COVERED_FLOOR`**. |
| C5 | Shares per contract | **100** (matches `OptionContract.multiplier` default). Non-100 equity multipliers are out of scope. |
| C6 | Helper home (DRY) | Move the canonical short-contract counter into **`AccountSnapshot.short_option_contracts(underlying, right)`**; `risk_core._short_option_contracts` becomes a thin delegator (one source of truth). |
| C7 | TOPUP (BUY) | **Untouched** — buying increases shares, never strips coverage. |

## 4. Architecture

A contained, two-file change. No new config; `RISK_REBALANCE_ENABLED` stays the
separate enable gate.

- **`autotrader/domain.py`** — new method on `AccountSnapshot`:
  `short_option_contracts(underlying: str, right: str) -> int`. Counts open short
  option contracts (negative qty) on `underlying` for the given right
  (`"CALL"`/`"PUT"`), parsing moomoo option codes. The logic moves verbatim from
  `risk_core._short_option_contracts` (including the `_OPT_SUFFIX` regex that
  guards prefix collisions — see §6).

- **`autotrader/risk_core.py`** — `_short_option_contracts(snapshot, underlying,
  right)` becomes a thin delegator to `snapshot.short_option_contracts(...)`. No
  behavior change to `_evaluate_option_leg` or `_evaluate`.

- **`autotrader/rebalance.py`** — `compute_plan` TRIM branch applies the coverage
  cap (see §5). Add module constant `SHARES_PER_CONTRACT = 100`.

## 5. The trim-sizing logic (`compute_plan`, TRIM branch only)

The coverage cap is applied **after** the existing within-band check, so the skip
reasons stay distinct:

```python
qty = int((current_value - target_value) // price)
qty = min(qty, current_qty)
if qty <= 0:
    skipped.append((symbol, "WITHIN_BAND")); continue
# reserve shares pledged to open short calls (covered call / collar)
covered_floor = snapshot.short_option_contracts(symbol, "CALL") * SHARES_PER_CONTRACT
qty = min(qty, max(0, current_qty - covered_floor))
if qty <= 0:
    skipped.append((symbol, "COVERED_FLOOR")); continue
if qty * price < cfg.rebalance_min_notional:
    skipped.append((symbol, "SKIPPED_MIN_NOTIONAL")); continue
trims.append(RebalanceTrade(symbol, "SELL", qty, "TRIM", current_qty - qty))
```

- `new_total_qty = current_qty - qty` is now always `≥ covered_floor`, so
  `consolidate_stop` resizes the trailing stop correctly and coverage holds
  post-trim.
- A new skip reason `COVERED_FLOOR` is added to the documented `OverlaySkip`-style
  reason set used for logging (`compute_plan` already emits `(symbol, reason)`
  tuples; the rebalance caller logs them at debug).

## 6. Edge cases & error handling

| Case | Behavior |
|---|---|
| No short calls (`covered_floor = 0`) | `free = current_qty` → **identical to today**; zero behavior change for non-covered names. |
| Fully pledged (100 shares, 1 short call) | `free = 0` → skip `COVERED_FLOOR`; coverage intact. |
| Partially pledged (150 shares, 1 short call) | trims at most 50; floor of 100 preserved. |
| Already under-covered (`current_qty < covered_floor`, shouldn't happen) | `max(0, negative) = 0` → skip `COVERED_FLOOR`; never sells into a deficit. |
| Prefix collision (`US.O` vs `US.OXY`) | `_OPT_SUFFIX` requires the suffix after the underlying prefix to start with 6 digits, so `US.OXY…`'s `"XY26…"` fails to match and is **not** counted as `US.O`. Logic moves verbatim to preserve this. |

No exceptions introduced — pure arithmetic over the snapshot.

## 7. Testing

**`domain.py` — `AccountSnapshot.short_option_contracts`:**
- Counts open short CALLs on the underlying; ignores long calls, all puts, and
  other underlyings.
- Prefix-collision: `US.OXY` short call is not counted for `US.O`.
- Returns 0 when no matching short options.
- (Parity) `risk_core._short_option_contracts` still returns the same values via
  the delegator — exercised by existing risk_core tests, which must stay green.

**`rebalance.py` — `compute_plan` coverage cap:**
- Covered position trims only down to the floor (150 shares + 1 short call,
  overweight → trims 50, `new_total_qty == 100`).
- Fully covered (100 shares + 1 short call, overweight) → skipped `COVERED_FLOOR`,
  no trade.
- Non-covered overweight position trims exactly as before (regression guard:
  unchanged behavior when no short calls).
- Coverage cap that drops the sellable notional below `rebalance_min_notional` →
  skipped `SKIPPED_MIN_NOTIONAL`.
- TOPUP path unaffected by short calls.

## 8. Out of scope

- **No `risk_core` evaluation change** — non-rebalance SELL paths (strategy
  stop-loss exits, external signals, emergency flatten) keep current behavior.
- **No auto-closing of option legs** — the rebalancer never closes a covering
  short call to free shares; it only declines to strip cover.
- **Non-100 option multipliers.**
- **Enabling rebalancing** — `RISK_REBALANCE_ENABLED` stays a separate
  human-reviewed change. Closing this gap is a precondition for that, not the act
  of flipping it.
