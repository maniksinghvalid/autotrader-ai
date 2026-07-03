# Strategy Book Segregation — Design

**Date:** 2026-07-03
**Status:** Approved (user-reviewed brainstorm, this document is the written record)
**Branch:** feat/live-readiness-v2

## Problem

The internal breakout engine (Stage 1) and the AI/rebalance book (Stages 2–3) share
one account with no notion of which strategy owns a position. Two concrete collisions
follow, both confirmed against the code:

1. **Rebalancer vs. breakout** — `rebalance.compute_plan` sees every owned position.
   A breakout entry made at 09:45 whose symbol carries a neutral/low AI score is
   trimmed or flattened by the 12:30 REBALANCE job, overriding the breakout engine's
   bracket exits (`main.py` `rebalance()` → `rebalance.py compute_plan`).
2. **Overlays vs. breakout** — an external overlay signal (e.g. covered call) on a
   symbol the breakout engine holds sells away exactly the right-tail upside the
   breakout entry was made to capture. Nothing in `_route_overlay` checks which book
   holds the shares.

A third, quieter collision falls out of the same root cause: `tick()` applies the
breakout bracket exits (stop/target) to *any* held position in the strategy symbol,
including one the AI book bought — the breakout engine can currently exit a position
it never entered.

Source: external "institutional validation" feedback (2026-07-03), triaged against
the codebase. Items confirmed valid; this design addresses the strategy-tagging /
book-segregation bundle only (see Out of Scope).

## Decisions (user-approved)

| # | Decision | Choice |
|---|----------|--------|
| D1 | Scope of this round | Strategy tagging + book segregation only |
| D2 | Ownership model | **Exclusive per-symbol claim** — a symbol belongs to exactly one book at a time; no lot-level accounting |
| D3 | External AI SELL on a breakout-owned symbol | **Skipped** with explicit reason — breakout brackets own the exit lifecycle. Safety exits always override |
| D4 | Unclaimed positions (pre-existing, manual, broker-sync restored) | **AI/rebalance book by default** — only an explicit breakout entry creates a claim |
| D5 | Mechanism | **Approach A: durable claims table + choke-point filters** (over trades-projection derivation and static disjoint universes) |
| D6 | Feature flag | **None — always-on.** Safety invariant, not a strategy toggle (precedent: overlay-intent failsafe guard). Zero-claims state is byte-for-byte today's behavior |

## Data model

New pure module **`autotrader/books.py`**: the `Origin` concept (`BREAKOUT`; enum-ready
for future books), skip-reason constants, and pure decision helpers that operate on a
claims mapping passed in. No SDK import, no DB handle. Added to the
`test_no_sdk_in_core.py` guard list.

New table in `db.py`, following the existing idempotent-migration pattern. Because
unclaimed = AI book (D4), absence of a row IS the AI-book state — the table only ever
stores breakout claims:

```sql
CREATE TABLE IF NOT EXISTS strategy_claims (
  symbol     TEXT PRIMARY KEY,   -- normalized code, e.g. US.NIO
  origin     TEXT NOT NULL,      -- 'BREAKOUT'
  claimed_at TEXT NOT NULL,
  session_id TEXT
)
```

`db.py` API: `claim_symbol(symbol, origin, session_id)`, `release_claim(symbol)`,
`get_claims() -> dict[str, str]`.

## Claim lifecycle

**Claim before submit.** The claim row is written after risk approval and immediately
BEFORE the router hands the breakout entry BUY to the broker. Rationale (mirrors the
audit-first router): ambiguity must default to "breakout owns it." If the process
crashes mid-order or the broker ack is UNKNOWN, the claim already exists and the AI
book keeps its hands off a position that might exist. A claim on a phantom position
costs one skipped rebalance; a real position with no claim is the commingling bug this
design exists to prevent. Ordering also gives fail-closed for free: if the claim write
fails, the entry order is not submitted.

**Release on confirmed flatness**, two independent paths:

1. Inline — the breakout exit SELL (stop/target/trail) confirms filled for the full
   position quantity.
2. Sync — ground-truth reconciliation (PRE_OPEN_SYNC, RISK_SWEEP, watchdog reconcile)
   observes qty 0 for a claimed symbol → release + log. This self-heals every other
   case: definitively rejected/cancelled zero-fill entries, halt-flatten, a broker-side
   trailing stop firing overnight, manual close in the Moomoo app.

A missed release fails conservative (symbol stays off-limits to the AI book until the
next sync pass). Restart requires no rebuild — claims are durable in SQLite alongside
halt state.

## Enforcement — four choke points, all in `TradeEngine` (`main.py`)

No changes inside `rebalance.compute_plan`, the strategy modules, or the overlay
planner internals; they stay pure.

1. **`tick()`** — manage breakout exit brackets only when the symbol is claimed
   BREAKOUT. A held-but-unclaimed position is left alone (logged once per session,
   not per tick). Entry on a held symbol is already impossible (strategy enters only
   when flat).
2. **`_route_signal()`** — external signal on a claimed symbol → skip with
   `BOOK_CONFLICT` result, for BOTH BUY and SELL (D3). Logged with signal id, same
   pattern as the overlay-intent guard.
3. **`rebalance()`** — the claimed set is subtracted from both the score/target
   inputs and the positions before `compute_plan` is called; a claimed symbol is
   invisible to the rebalancer. Skips logged as `BOOK_CLAIMED`.
4. **`_route_overlay()`** — claimed underlying → `OverlaySkip("OVERLAY_BOOK_CONFLICT")`.

**Safety paths deliberately ignore claims:** `_flatten_all`, StopManager
reconciliation, broker-resting trailing stops, cancel-on-shutdown. Claims constrain
strategy decisions, never safety decisions.

## Error handling

- Claim write fails → entry not submitted (ordering guarantees this); logged as
  error; tick returns a failure result. No new alert channel.
- Release fails after a confirmed exit fill → log-only, never blocks the exit; the
  sync path retries. Stale claim = conservative.
- Restart mid-anything → durable claims + next sync reconcile.

## Observability

Lean: INFO line on claim and release, skip reasons in existing logs, claims readable
via `get_claims()`. No EOD-report changes this round.

## Testing (TDD, ~16–20 tests)

- `books.py` pure decision tests.
- DB: claim CRUD, idempotent migration, restart persistence.
- Lifecycle: claim exists before submit (including UNKNOWN-ack case); release on full
  exit fill; release on sync-observed flatness; zero-fill rejection cleanup.
- Enforcement: tick leaves unclaimed positions alone; external BUY and SELL skipped
  on claimed symbols; rebalance excludes claimed symbols from targets AND positions;
  overlay skip on claimed underlying.
- Safety: `_flatten_all` still sells claimed positions.
- Regression: zero-claims state reproduces today's behavior exactly.

## Out of scope (triaged, deliberately deferred)

From the same feedback round, not in this design:

- Exit-engine overhaul (remove hard 10% take-profit, ATR-scaled stops).
- Delta-adjusted option exposure accounting in the risk core.
- Morning risk-check job + open-stabilization window (note: the feedback's "gap-down
  liquidation at the bell" scenario is factually impossible today — tiered halt checks
  run only at 13:30/15:00; the real gap is the *absence* of any system-level risk
  check between 09:45 and 13:30).
- Forcing LIMIT orders unconditionally for multi-leg overlays (already available
  behind `RISK_LIMIT_ORDERS_ENABLED`).
