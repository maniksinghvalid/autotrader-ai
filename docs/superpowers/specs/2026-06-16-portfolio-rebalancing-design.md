# Portfolio Rebalancing + Midday Risk Re-check — Design Spec

**Date:** 2026-06-16
**Branch:** `feat/autotrader-paper-v1`
**Status:** Approved design — ready for implementation plan
**Scope:** Add true portfolio rebalancing (signal-score target weights + drift bands),
a tiered midday risk re-check, and trailing-stop consolidation on position-qty changes.
Paper-only; every invariant in CLAUDE.md held.

## 1. Goal

Extend the existing lifecycle from *per-position management* to *portfolio-level
maintenance*: hold each symbol near a conviction-driven target weight (growth),
and add intraday loss/exposure preservation, without violating any safety
invariant (paper-only, single audited order path, no exposed OpenD socket, risk
limits in `config/`).

## 2. Decisions (locked during brainstorming)

| # | Decision | Choice |
|---|----------|--------|
| D1 | Target weight source | **Signal-score driven** — renormalized composite scores from the daily sweep |
| D2 | Schedule | `REBALANCE` @ 12:30, `RISK_CHECK` @ 13:30 + 15:00 (on top of the existing 4 jobs) |
| D3 | Rebalance action | **Two-sided** — trim overweight (partial SELL) + top-up underweight (BUY) |
| D4 | Midday risk re-check | **Tiered** — close gate first, flatten + halt if loss worsens past a hard limit |
| D5 | Target delivery | Optional `portfolio_targets[]` block on the **existing** `RoutineSignalPayload` — no new ingress, no webhook security change |
| D6 | Untargeted holdings | A position absent from the target snapshot is **left untouched** (managed by its strategy/trailing stop), never force-sold to zero |
| D7 | Stop consolidation | **In scope** — re-size the trailing stop on every rebalance qty change |
| D8 | Exits during a loss breach | `risk_core` gains a **reduce-only exception**: a position-reducing SELL bypasses the risk-increasing caps (daily-loss/notional/exposure) so the hard-tier flatten and strategy stop-loss exits can liquidate during a breach. Env/stale/allow-list/long-only still apply. |
| D9 | Partial-coverage weighting | **Allow-list-diversified**: when only some allow-listed symbols score, the investable pool is scaled by `n_scored / n_allowed` — a single scored name gets at most `investable / N_allowed`, the rest stays in cash. Caps concentration (preservation-leaning); does NOT award unscored symbols' share to scored ones. |

## 3. Architecture

```
DAILY SWEEP (cloud /trade-routine)
   └─ RoutineSignalPayload { signal_changes[], + portfolio_targets[] }
        └─ inbox / webhook ──ingest──▶ runner
                                        ├─ signal_changes → submit_external_signal   (unchanged)
                                        └─ portfolio_targets → TargetWeightStore (DB)

LIFECYCLE CLOCK (America/New_York)
  08:30 PRE_OPEN_SYNC ─ 09:45 ENTRY_OPEN ─ ▶12:30 REBALANCE◀ ─ ▶13:30 RISK_CHECK◀
       ─ ▶15:00 RISK_CHECK◀ ─ 15:30 RISK_SWEEP ─ 16:15 EOD_FLATTEN

REBALANCE  → rebalance.compute_plan(snap, targets, prices, cfg)
               → engine.submit_rebalance_order(req)   → risk_core → OrderRouter → broker + DB
               → engine.consolidate_stop(symbol, new_qty, price, round)  (per qty change)
RISK_CHECK → risk_check.evaluate(snap, cfg)
               → soft: gate.close()
               → hard: flatten (audited SELLs) + cancel_all + db.record_halt + halt flag
```

Three new pure-compute modules (`rebalance.py`, `risk_check.py`, target-weight
helpers), zero new network surface, every trade through the existing single
audited path (`risk_core.evaluate → OrderRouter.submit → db.record_trade`).

## 4. Components

### 4.1 Target-weight store (`autotrader/signals/schema.py` + `autotrader/portfolio.py`)

- Extend `RoutineSignalPayload` with optional `portfolio_targets: list[TargetWeight]`
  where `TargetWeight = {symbol, score}`. The sweep already computes composite
  scores; the adapter carries the **levels** (today it carries only *changes*).
- On ingest, the runner extracts `portfolio_targets` into a DB-backed
  `TargetWeightStore` (new `target_weights` table: `symbol, score, as_of_date,
  ingested_at`). Latest snapshot per `as_of_date` wins.
- `TargetWeightStore.latest()` → `{symbol: score}` plus the snapshot timestamp.
- **Staleness guard:** if the latest snapshot is older than
  `RISK_TARGET_STALENESS_HOURS` (default 24), `REBALANCE` logs and no-ops. Never
  act on stale targets.
- Symbols are qualified against `RISK_ALLOWED_SYMBOLS` (same allow-list the
  adapter already enforces); unknown symbols are dropped with a warning.

### 4.2 `rebalance.py` — pure compute (no broker access, SimBroker-testable)

`compute_plan(snapshot, targets, prices, cfg) -> RebalancePlan`

- `targets`: `{symbol: score}` from the store. Scores renormalized to weight
  fractions of **investable** equity, where
  `investable = total_assets × (1 − RISK_REBALANCE_CASH_BUFFER_PCT/100)`,
  then scaled by `n_scored / n_allowed` (decision D9 — allow-list-diversified:
  a single scored name gets at most `investable / N_allowed`, the remainder
  stays in cash; unscored symbols' share is not awarded to scored ones).
- `prices`: current quote per symbol (market value uses live quote, not
  `avg_price`).
- Per symbol in the target set:
  - `current_weight = position_qty × price / total_assets`
  - `drift = current_weight − target_weight`
  - within `±RISK_REBALANCE_BAND_PCT` (percentage points) → skip (within band)
  - overweight → partial **SELL** `round((current_value − target_value)/price)`
  - underweight → **BUY** `round((target_value − current_value)/price)`
  - skip any trade whose notional `< RISK_REBALANCE_MIN_NOTIONAL` (anti-churn)
- **Ordering:** trims emitted before top-ups, so cash frees up and the gross
  exposure cap is not transiently breached mid-round.
- **D6:** positions with no target are omitted from the plan entirely.
- Returns an ordered `list[OrderRequest]` plus per-symbol annotations
  (`WITHIN_BAND` / `TRIM` / `TOPUP` / `SKIPPED_MIN_NOTIONAL`) for logging.
- `RebalancePlan` is a value object; the rebalancer **never** touches the broker.

### 4.3 Audited rebalance submit (`autotrader/main.py` — `TradeEngine`)

`submit_rebalance_order(req: OrderRequest) -> TickResult`

- Routes `risk_core.evaluate → OrderRouter.submit → db.record_trade` — identical
  audit/DB guarantees as `_route_signal`, but:
  - honors `EntryGate` for BUY top-ups (gate must be open; at 12:30 it is — a
    prior `RISK_CHECK` gate-close correctly blocks top-ups);
  - allows **explicit partial qty** SELLs (bypasses the "SELL = liquidate full
    position" shortcut in `_route_signal`, **never** the risk core);
  - cid tagged `rbal-<round_id>` for idempotency.
- The rebalancer proposes; **`risk_core` remains the sole pass/reject gate.** A
  top-up that would breach notional/position/exposure caps is rejected and
  logged — the round continues with the remaining trades.

### 4.4 Stop consolidation (`autotrader/main.py` — `TradeEngine`)

`consolidate_stop(symbol, new_total_qty, ref_price, round_id) -> None`, called
after every rebalance trade that changes a symbol's qty:

1. **Find** the symbol's working `TRAILING_STOP` SELL via new
   `db.get_open_trailing_stop(symbol)` (state in {SUBMITTED, PARTIAL}).
2. **Cancel** it targeted — `broker.cancel_order(boid)` — and
   `db.mark_order_cancelled(boid)` so the open-stop query stays accurate.
3. If `new_total_qty > 0`: place one fresh `TRAILING_STOP` SELL for
   `new_total_qty` through the audited path, cid =
   `make_client_order_id(symbol, "SELL", new_total_qty, f"{round_id}-stop")`
   (idempotent — re-runs dedupe at the router/sim).
4. If `new_total_qty == 0` (position fully trimmed) → cancel only, no re-place.

**Live-vs-sim correctness:** `new_total_qty` is computed deterministically as
`current_qty ± the rebalance order qty just submitted`, **not** a re-fetched
snapshot. On live OpenD a MARKET top-up returns `SUBMITTED` before the fill
lands, so a re-fetched snapshot would under-size the stop; sizing off the
intended post-trade qty is deterministic and unit-testable against `SimBroker`.

The same helper can later replace the qty-only `_attach_trailing_stop` attach in
the entry flow — noted as a **non-blocking follow-up**, not part of this change.

### 4.5 Tiered risk re-check (`autotrader/risk_check.py`)

`evaluate(snapshot, cfg, gate, halt_state) -> RiskCheckResult`, called at 13:30
and 15:00:

- `day_pnl ≤ −RISK_DAILY_LOSS_LIMIT` (soft) → `gate.close()` (no new BUYs);
  existing trailing stops are kept. Logged `RISK_GATE`.
- `day_pnl ≤ −RISK_DAILY_LOSS_HALT` (hard, `> soft`) → flatten all positions
  (audited SELLs through `submit_rebalance_order`/full-exit path) + `cancel_all`
  + `db.record_halt(reason)` + set the session **halt** flag. Logged `RISK_HALT`.
- `RISK_DAILY_LOSS_HALT > RISK_DAILY_LOSS_LIMIT` is validated at config load;
  a misordered pair is a startup error.

**Reduce-only exit exception (D8):** the hard-tier flatten places SELLs through
the audited path, but `risk_core` currently rejects *all* orders once
`day_pnl <= -daily_loss_limit`. So `risk_core.evaluate` gains a minimal-diff
exception: a SELL that strictly reduces a long position (`0 <= resulting < held`)
skips the three risk-increasing caps (daily-loss, order notional, gross
exposure); env / stale / allow-list / long-only checks still apply. This keeps
the single audited path intact (exits are still evaluated, just never blocked for
a risk-increasing reason) and also fixes strategy stop-loss exits during a
breach. All existing `risk_core` tests remain green (no check reordering).

**Soft-tier scope note:** v1 closes the gate only (entries are MARKET orders that
fill immediately in sim and rest only briefly live); it does **not** selectively
cancel resting BUY limit orders, because v1 places none. When limit entries are
added, the soft tier should also cancel working BUY orders via the existing
per-order `broker.cancel_order` — recorded here as a forward note.

### 4.6 Session halt flag (`autotrader/lifecycle.py`)

A small shared `SessionState` (or extend `EntryGate`) holding `entries_enabled`
and `halted`. When `halted` is set: `tick()`, `submit_external_signal`,
`submit_rebalance_order`, and the `REBALANCE` job all return `HALTED` without
trading until the next session. `db.record_halt` persists it (existing `halts`
table); `EOD_FLATTEN` already halts conceptually for the day.

### 4.7 Scheduler (`autotrader/scheduler.py`)

Add three entries, keeping `_SCHEDULE` chronologically sorted (load-bearing):

```
PRE_OPEN_SYNC 08:30, ENTRY_OPEN 09:45, REBALANCE 12:30,
RISK_CHECK_MID 13:30, RISK_CHECK_LATE 15:00, RISK_SWEEP 15:30, EOD_FLATTEN 16:15
```

The two risk checks have distinct job names (the scheduler keys once-per-day
*per name*) but `runner._run_job` dispatches both to one `_run_risk_check`
handler. Catch-up-on-late-start semantics are unchanged.

### 4.8 Config (`config/risk.config.example`) — all human-reviewed risk limits

```
RISK_REBALANCE_ENABLED=false          # opt-in; default off
RISK_REBALANCE_BAND_PCT=5.0           # ±5 percentage points of weight
RISK_REBALANCE_MIN_NOTIONAL=200       # skip trades smaller than this (anti-churn)
RISK_REBALANCE_CASH_BUFFER_PCT=10.0   # reserve as cash; weights apply to the rest
RISK_TARGET_STALENESS_HOURS=24        # skip rebalance if the snapshot is older
RISK_DAILY_LOSS_HALT=1000             # hard flatten+halt (soft reuses RISK_DAILY_LOSS_LIMIT=500)
```

Changing any of these requires explicit human review (CLAUDE.md). `RiskConfig`
in `autotrader/config.py` gains the matching fields with safe defaults so an
un-updated config still loads (rebalancing simply stays disabled).

### 4.9 DB (`autotrader/db.py`)

- New `target_weights` table: `symbol, score, as_of_date, ingested_at`.
- New `DB.upsert_target_weights(snapshot)`, `DB.latest_target_weights()`.
- New `DB.get_open_trailing_stop(symbol)` and `DB.mark_order_cancelled(boid)`.
- Rebalance trades reuse the existing `trades` table, distinguished by the
  `rbal-<round>` cid tag; halts reuse the existing `halts` table.

## 5. Invariants preserved

- **Paper-only:** no live path added; `RISK_REBALANCE_ENABLED` default off.
- **Single audited order path:** rebalance/flatten/stop orders all go through
  `risk_core.evaluate → OrderRouter.submit` (audit-before-place, idempotent cid).
- **Risk core is the sole gate:** the rebalancer only *proposes*; caps are
  enforced by `risk_core`, unchanged.
- **No exposed socket:** target weights ride the existing authenticated payload;
  the webhook contract and its HMAC auth are untouched (only the optional schema
  field is added, validated by the same Pydantic model).
- **No SDK in core:** `rebalance.py` and `risk_check.py` import stdlib +
  `autotrader.domain`/`config` only — covered by extending `test_no_sdk_in_core`.
- **Deterministic + testable:** all new logic is pure or injected-clock driven
  and runs against `SimBroker` with no OpenD.

## 6. Testing

- **`rebalance`:** within-band no-op; trim qty math; top-up qty math; min-notional
  skip; cash-buffer reservation; trims-before-top-ups ordering; untargeted
  position left alone (D6); score renormalization; empty/stale target set.
- **`risk_check`:** soft gate (gate closes, stops kept); hard flatten+halt
  (positions flattened, `cancel_all`, `record_halt`, halt flag set); soft<hard
  config validation.
- **stop consolidation:** top-up grows the stop (old cancelled, new = full qty);
  trim shrinks it; full trim cancels with no re-place; idempotent re-run (no
  duplicate stops); cancelled stops excluded from `get_open_trailing_stop`.
- **scheduler:** new jobs fire at the right times, once/day, with late-start
  catch-up; chronological ordering preserved.
- **runner integration (SimBroker):** full midday sequence end-to-end
  (REBALANCE then RISK_CHECK); halt flag stops subsequent trading.
- **target store:** ingest `portfolio_targets`, latest-wins, staleness guard.
- **invariants:** extend `test_no_sdk_in_core`; assert rebalance trades produce
  audit lines and pass through `risk_core`.

## 7. Out of scope (explicit)

- Replacing the entry flow's `_attach_trailing_stop` with `consolidate_stop`
  (follow-up; this change only adds the helper and uses it in rebalancing).
- Selective cancel of resting BUY **limit** orders in the soft risk tier (v1
  places no limit entries).
- Any live-trading path. v1 stays paper-only.
- Score→weight models beyond linear renormalization (e.g. softmax, risk parity).
