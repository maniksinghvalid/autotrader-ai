# Rebalance Order-Slicing (Cap-Aware Top-Up Sizing) — Design

**Date:** 2026-06-23
**Status:** Approved (design); implementation pending
**Repo of change:** `AutoTrader`
**Related:** [`2026-06-16-portfolio-rebalancing-design.md`](2026-06-16-portfolio-rebalancing-design.md), [`2026-06-22-covered-share-reservation-design.md`](2026-06-22-covered-share-reservation-design.md).

## 1. Problem

With `RISK_REBALANCE_ENABLED=true` and targets flowing, the first live rebalance
(2026-06-23) rejected **every** order:

```
WARNING rebalance rejected BUY US.CLOV 13527: order notional 68784.79 > cap 5000.0
... (7 symbols, all REJECTED_BY_RISK)
```

`compute_plan` sizes each top-up to close the **entire** drift in one order
(`qty = (target_value − current_value) // price`). On a ~$1M book that is 95% cash
with tiny holdings, each top-up is $50k–75k — 10–15× the `RISK_MAX_ORDER_NOTIONAL`
of $5,000. `risk_core.evaluate()` **rejects** (it does not clamp), so every order
bounces and the portfolio never moves toward target. Confirmed: 0 rebalance trades
placed.

Three caps reject a risk-increasing BUY, in `evaluate()` order:
1. `RISK_MAX_ORDER_NOTIONAL` (5000) — per-order notional.
2. `RISK_MAX_POSITION_QTY` (100) — **resulting** position qty.
3. `RISK_MAX_GROSS_EXPOSURE` (100000) — projected gross after the order.

Notional is hit first today, but the qty cap is a hard wall: a name can never
exceed 100 shares regardless of slicing, so slicing the notional alone is
insufficient.

## 2. Goal & non-goal

**Goal:** size each rebalance top-up so it **passes all three caps and places**,
moving the position toward target up to the cap envelope, and converging across the
daily 12:30 rounds — instead of being rejected outright.

**Non-goal:** reaching score-weighted target *values* that exceed the caps. With
`max_position_qty=100`, each name's reachable allocation is bounded by the caps,
not the target. Raising the caps to fully deploy the book is a **separate,
human-reviewed risk-limit change** (CLAUDE.md) and is explicitly out of scope. This
spec makes the rebalancer *functional within the current risk envelope*.

## 3. Key decisions

| # | Decision | Choice |
|---|----------|--------|
| S1 | Caps the slicer sizes against | **All three** — `max_order_notional`, `max_position_qty` (resulting), and remaining `max_gross_exposure` headroom. Orders always place; never `REJECTED_BY_RISK` on these caps. |
| S2 | Structure | **One clamped order per symbol per round.** Convergence happens across the daily 12:30 rounds. No multi-slice bursts, no executor/submit-loop change. |
| S3 | Layer | **`compute_plan` only** (pure). No `risk_core` or executor change; `evaluate()` remains the backstop. |
| S4 | Scope | **TOPUP/BUY only.** Trims are reduce-only SELLs; `evaluate()` already skips the notional/qty/gross caps for reduce-only orders (D8 exception), so trims place and need no clamp. The covered-floor cap on trims (prior spec) is unchanged. |
| S5 | Gross tracking | Seed `running_gross = snapshot.gross_exposure()`; increment by each top-up's notional as it is added, in the existing deterministic `sorted(fractions)` order. **Conservative:** the round's trims (which execute first and free real gross headroom) are NOT subtracted — under-deploying slightly is the safe direction and avoids coupling the two branches. |
| S6 | No-headroom outcome | **Skip** with new reason `CAPPED` (already at qty cap, or gross exhausted by earlier top-ups this round) — never emit a zero/over-cap order. |

## 4. The clamp logic (TOPUP branch of `compute_plan`)

`compute_plan` already iterates `sorted(fractions)` deterministically. Seed
`running_gross = snapshot.gross_exposure()` before the loop. Replace the TOPUP
branch sizing with a clamp against all three caps:

```python
else:  # underweight -> top up
    qty = int((target_value - current_value) // price)
    if qty <= 0:
        skipped.append((symbol, "WITHIN_BAND"))
        continue
    # Clamp to the largest qty that passes all three risk caps, so the order
    # places (and converges across rounds) instead of being rejected.
    notional_cap_qty = int(cfg.max_order_notional // price)
    qty_cap_room     = max(0, cfg.max_position_qty - current_qty)
    gross_room_qty   = int(max(0.0, cfg.max_gross_exposure - running_gross) // price)
    qty = min(qty, notional_cap_qty, qty_cap_room, gross_room_qty)
    if qty <= 0:
        skipped.append((symbol, "CAPPED"))
        continue
    if qty * price < cfg.rebalance_min_notional:
        skipped.append((symbol, "SKIPPED_MIN_NOTIONAL"))
        continue
    topups.append(RebalanceTrade(symbol, "BUY", qty, "TOPUP", current_qty + qty))
    running_gross += qty * price
```

- `running_gross` is initialized once before the symbol loop and incremented only
  when a top-up is appended.
- The TRIM branch is unchanged (covered-floor cap stays as-is).
- New skip reason `CAPPED`; `WITHIN_BAND` and `SKIPPED_MIN_NOTIONAL` semantics are
  unchanged.

## 5. Behavior & convergence

- Each round, every underweight allow-listed name gets one order clamped to
  `min(drift_qty, notional/price, max_position_qty − current, gross_room/price)`.
- Because the resulting-position qty cap bounds the position, a name reaches its
  cap ceiling in roughly one round; further rounds top up only as drift reopens.
- When the round's cumulative top-ups exhaust gross headroom, the remaining
  (sorted-later) names are skipped `CAPPED` that round and reconsidered next round.
- Orders that previously logged `REJECTED_BY_RISK` now **place** at the clamped qty.

## 6. Edge cases & error handling

| Case | Behavior |
|---|---|
| Drift smaller than every cap | `qty` = full drift, unchanged from today (clamp is a no-op). |
| `price > max_order_notional` (single share over notional cap) | `notional_cap_qty = 0` → clamp 0 → skip `CAPPED`. (Risk core would also reject; skipping avoids a futile order.) |
| Position already at `max_position_qty` | `qty_cap_room = 0` → skip `CAPPED`. |
| Gross headroom exhausted by earlier top-ups | `gross_room_qty = 0` → skip `CAPPED`. |
| Clamped qty places but is below `rebalance_min_notional` | skip `SKIPPED_MIN_NOTIONAL` (existing behavior, checked after clamp). |
| `gross_exposure()` already ≥ cap | every top-up clamps to 0 → all `CAPPED`; no orders. |

Pure arithmetic over the snapshot/cfg; no exceptions introduced.

## 7. Testing

`tests/test_rebalance.py`:
- **Notional clamp:** an underweight name whose full-drift order exceeds
  `max_order_notional` is emitted at `floor(max_order_notional/price)` (e.g. price
  100, cap 5000 → qty 50), `new_total_qty` consistent. (Regression vs the rejected
  68k-notional behavior.)
- **Position-qty clamp:** a name with `current_qty` near `max_position_qty` tops up
  only to the cap (e.g. current 90, cap 100 → qty 10).
- **Gross-headroom clamp:** two underweight names where the first consumes most of
  `max_gross_exposure`; the second is clamped to the remaining headroom (and skipped
  `CAPPED` if zero). Asserts running-gross is tracked in sorted order.
- **CAPPED skip:** name already at `max_position_qty` → skipped `CAPPED`, no trade.
- **No-op when small:** a drift within all caps trims/tops-up exactly as before
  (regression guard — clamp doesn't change sub-cap orders).
- **min-notional after clamp:** clamp reduces a top-up below `rebalance_min_notional`
  → `SKIPPED_MIN_NOTIONAL`.
- **Trims unaffected:** an overweight covered/uncovered name trims exactly as before
  (clamp is TOPUP-only).

## 8. Out of scope

- **Raising any risk cap** (`max_order_notional`, `max_position_qty`,
  `max_gross_exposure`) — human-reviewed risk-limit change; the caps deliberately
  bound the reachable allocation here.
- **Multi-slice (multiple orders per symbol per round)** — the qty cap makes ~one
  order per symbol sufficient; not worth the order-burst + submit-loop change.
- **Trim sizing / reduce-only paths** — unchanged.
- **Modeling trim-freed gross headroom within the same round** (S5 conservative
  simplification).
- **`risk_core` / executor changes** — `evaluate()` stays the backstop.
