# Design — Report Integrity, Overlay Hedge Integrity, and Protective Limit Orders

**Date:** 2026-06-30
**Branch:** `feat/autotrader-paper-v1`
**Status:** Approved (brainstorm) — pending spec review, then implementation plans.

## Origin

Triggered by review of the Tue Jun 30 2026 (PAPER) EOD session summary, which showed
several internally inconsistent values. Codebase exploration separated genuine bugs
from working-as-designed behavior:

- **`Gross exp. 0%` with an open NVDY long** — bug (stale/empty position snapshot).
- **`Realized +0.00` / `Unrealized +0.00` on an exit-heavy day** — misleading
  (broker fields null → silent default to 0).
- **`Cash 1,001,019 > Total assets 1,000,569`** — *not* a new bug: consistent with
  open short-option liabilities (covered calls / collar short calls) at the account
  level, independent of the failed positions list.
- **MARA "Bought 100 / Sold 200"** — *not* a naked short: `risk_core.py:107` rejects
  any sell that would take a position negative (long-only guard, tested). Explained by
  a pre-existing 100-share long being doubled then fully exited to flat.
- **"Protective Put / Collar (options overlay)" thesis on lines labeled "Stock entry"**
  — the structural classifier (fills-based) says stock-only while the thesis text
  (signal rationale, recorded pre-execution) says hedged. The option legs did not fill;
  these are naked stock wearing a hedged thesis line.
- **All orders are naked MARKET** (`main.py:128,276`, `planner.py:111`); only the
  trailing-stop exit is non-market. The paper `SimBroker` fills at the snapshot quote
  with **zero slippage** (`sim_broker.py:50`), so execution quality is currently
  unmeasurable in paper.

Three independent-but-sequenced workstreams follow. Order matters: report integrity
first (nothing else can be validated until the report is trustworthy), overlay hedge
integrity second (the biggest safety gap, same reporting code), protective limit
orders last (biggest behavior change; needs the slippage model + a trustworthy report
to prove it helps).

Related prior spec: `docs/superpowers/specs/2026-06-19-overlay-intent-failsafe-guard-design.md`.

---

## §1 — Report Integrity

**Goal:** the EOD header never shows fabricated or silently-defaulted numbers; a
missing value is visibly missing; P&L is computed from our own fills/positions so
paper days are meaningful.

### A. Stale-snapshot guard (fixes `Gross exp. 0%` and the inherited-stale header)

- Distinguish **"positions genuinely empty"** from **"positions failed to load"** in
  the broker account fetch. Today both collapse to `positions=()`
  (`moomoo_broker.py:286-308`, `_positions()` returns `None` on rate-limit/failure →
  `AccountSnapshot(positions=())`). Carry an explicit `positions_loaded: bool`.
- At EOD, if positions failed: **retry with exponential backoff** (reuse the existing
  readiness/backoff pattern — never `time.sleep`). If still failed, record the row
  with `gross_exposure = NULL` and `stale = True`.
- `record_performance()` (`db.py:211`) gains an **invariant check**: refuse to write
  `gross_exposure = 0` when `positions_loaded` is false.
- Reporter renders `gross_exposure IS NULL` as **"exposure unavailable — snapshot
  stale"**, never `0%`.

### B. Compute realized/unrealized ourselves (fixes `+0.00`)

- **Realized (day):** derive from the `fills` table using a **running average-cost
  basis per symbol** built from full fill history in our DB (self-contained, matches
  the existing `Position.avg_price` model — decided over FIFO lot matching, which would
  need a lot ledger and schema change). For each SELL today:
  `realized += (sell_price − avg_cost) × qty`. Fall back to broker `realized_pl` only
  if our fills history is insufficient.
- **Unrealized:** `Σ open_position.qty × (current_quote − avg_cost)` using the same
  snapshot. If the snapshot is stale (per §1.A), unrealized renders **"unavailable"** —
  it cannot be trusted without positions.
- Display shows a real computed number, and `—`/"n/a" only when genuinely
  underivable — never a defaulted `0.00`.

### C. Capital-flow sign/label (fixes `Net cash deployed +8,665`)

- The value is correct; the label is inverted. Positive net (SELL > BUY) = cash
  **raised** (`capital_flow.py`: `net += dollars if SELL else −dollars`). Render the
  verb by sign: **"Net cash raised +N"** when positive, **"Net cash deployed −N"** when
  negative. No formula change.

### §1 Testing (all offline, no live OpenD)

- Stale → NULL + retry path; `record_performance` invariant rejects a false 0%.
- Realized/unrealized computed from a seeded fills+positions fixture.
- Capital-flow label flips correctly by sign.

---

## §2 — Overlay Hedge Integrity

**Goal:** the system never *silently* holds naked stock meant to be hedged, and the
report distinguishes intended overlay from what actually filled.

### A. Pre-check hedge before stock (execution)

- In `_route_overlay` (`main.py:450`), validate the **hedge legs (option puts /
  short-calls) as a group first**: contract resolved, live quote present, and
  `risk_core.evaluate()` approved — *before* the stock leg is submitted. If the hedge
  can't be placed, **do not buy the stock** (return a skip; no naked entry). This
  inverts today's "longs-first, residual-on-reject" ordering for the stock leg.

### B. Fallback + alert when hedge isn't confirmed (execution)

- **"Confirmed hedge"** = the hedge order acknowledged **FILLED** (not merely
  submitted). If the stock leg fills but the hedge doesn't confirm (rejected, or
  unfilled in paper):
  1. **Attach the protective trailing-stop** to the stock as a fallback risk control
     (reuse the attach path at `main.py:246`), and
  2. Fire an **immediate Slack UNHEDGED alert** (execution-time, not just EOD) naming
     the symbol, intended overlay, and reason.
- **Plan risk to confirm first:** whether the paper broker fills option MARKET orders
  at all. If it never does, every paper overlay trips the fallback — verify before
  wiring, so the alert is meaningful.

### C. Intended-vs-actual reporting

- The reporter already has both signals: the **signal rationale** (carries the
  `PROTECTIVE_PUT:` / `COLLAR:` intent prefix, `eod_reporter.py:90-93`) and the
  **structural `classify_strategy()`** label (fills-based, `classify.py:19-46`). Add a
  reconciliation: when the intent prefix indicates an overlay but the structural label
  lacks the corresponding option leg, render:
  `Stock entry — O ⚠ INTENDED: Protective Put — HEDGE LEG MISSING (unhedged)`.
- Consistent with the existing architecture (reporter is SDK/broker-free, DB-only,
  parse/structural inference — no new persistence required).

### §2 Testing (all offline)

- Pre-check blocks stock entry when the hedge leg is unplaceable.
- Stock-filled-but-hedge-unconfirmed → trailing-stop attach + captured UNHEDGED alert.
- Reporter emits the ⚠ intended-vs-actual annotation from a seeded signals+fills
  fixture where intent ≠ fills.

---

## §3 — Protective (Marketable) Limit Orders + Paper Slippage Model

**Goal:** replace naked MARKET orders with price-capped marketable limits that still
guarantee execution on risk exits, and make the paper broker model spread/slippage so
the improvement is measurable.

### A. Slippage-aware SimBroker (build first — the measuring stick)

- Today `SimBroker` fills instantly at the snapshot quote with zero slippage
  (`sim_broker.py:50`). Add a **spread model**: derive a simulated bid/ask around the
  reference (configurable spread, optionally per-price-tier). **MARKET** fills at the
  far touch (buy@ask, sell@bid) plus optional slippage; **LIMIT** fills only if
  marketable against the simulated touch, otherwise rests/unfilled. This exposes the
  cost difference between order types in paper.

### B. Capped marketable-limit construction

- Helper computes limit from the touch + cap: SELL → `bid − cap`, BUY → `ask + cap`,
  where `cap = max(RISK_ORDER_CAP_BPS × price, RISK_ORDER_CAP_TICKS × tick)`. The bps
  term governs normal names; the tick floor protects low-priced/thin names
  (CLOV $5.23, MARA $13.60).
- **Plan risk to confirm:** whether the live quote exposes true bid/ask. If only
  `last`/mid is available, cap is applied around that.
- Wire into the three submission points: strategy signals (`main.py:128`), rebalance
  (`main.py:276`), option legs (`planner.py:111`). **Trailing-stop exits stay
  broker-resting/unchanged.** Plumbing already exists: `OrderRequest.limit_price` and
  broker `LIMIT → OrderType.NORMAL` (`moomoo_broker.py:206-213`).

### C. Re-peg-then-market escalation

- After submitting a capped limit, check fill status within a bounded window; if
  unfilled, **cancel + re-submit once** pegged to the current touch; if still
  unfilled, **submit MARKET** so the order (especially a risk exit) completes.
  Requires per-order status/cancel — verify `modify`/`cancel` support in the plan.

### D. Config + rollout (human-reviewed risk params)

- New config keys: `RISK_ORDER_CAP_BPS`, `RISK_ORDER_CAP_TICKS`,
  `RISK_LIMIT_ORDERS_ENABLED` (default **false** → current MARKET behavior). Per
  CLAUDE.md, these are risk parameters — they live in `config/` and require explicit
  human review. Enable capped-limit only after the slippage model shows it improves
  realized fills in paper.

### §3 Testing (all offline)

- Slippage model: MARKET pays the spread; marketable LIMIT fills at cap;
  non-marketable LIMIT rests.
- Cap math including tick-floor on a low-priced name.
- Escalation path: unfilled → re-peg → market.
- Flag off = byte-for-byte current behavior.

---

## Decisions log (from brainstorm)

| Topic | Decision |
|---|---|
| Sequence | §1 report integrity → §2 overlay hedge integrity → §3 limit orders |
| Stale snapshot | Retry w/ backoff, then record with `gross_exposure=NULL` + stale flag; never emit false 0% |
| P&L source | Compute realized (running avg-cost from fills) + unrealized (positions × quote); broker as fallback |
| Cost-basis method | Average cost (not FIFO) |
| Capital-flow label | Verb by sign: "raised" (+) / "deployed" (−); no formula change |
| Hedge failure (execution) | Pre-check hedge placeable before stock; if stock fills unhedged → trailing-stop fallback + immediate Slack UNHEDGED alert |
| Report of partial overlay | Intended-vs-actual ⚠ annotation on the trade line |
| Limit non-fill | Re-peg once to touch, then fall back to MARKET |
| Price cap | `max(X bps, N ticks)`, config-driven, human-reviewed |
| Rollout | Ship slippage model first; capped-limit behind `RISK_LIMIT_ORDERS_ENABLED` defaulting to current MARKET behavior |

## Non-goals

- FIFO/tax-lot accounting.
- Changing risk-limit *values* (caps, loss thresholds) — only adding the new order-cap
  params, subject to human review.
- Exposing OpenD or the order path to the internet; no change to the webhook ingress.
- LIVE trading — everything remains paper (`TrdEnv.SIMULATE`) per project rules.

## Open items to resolve during planning (not blockers)

1. Does the paper broker fill option MARKET orders? (Gates §2.B meaning.)
2. Does the live quote expose true bid/ask, or only last/mid? (Shapes §3.B.)
3. Per-order `modify`/`cancel` support for the §3.C escalation.
