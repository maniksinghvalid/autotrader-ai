# Pre-Live Hardening — Design

**Date:** 2026-07-02
**Branch:** feat/autotrader-paper-v1
**Source:** Full-codebase review of 6135b89..0f00e05 (all `autotrader/` modules read; 519 tests collect). Verdict: merge with fixes — 1 Critical, 7 Important, 10 Minor findings.
**Goal:** Close every live-path gap before the first live cutover, which the user intends to run with **all four subsystems enabled**: core signals → stock orders, protective limit orders (`RISK_LIMIT_ORDERS_ENABLED`), options overlays, and rebalancing (`RISK_REBALANCE_ENABLED`).

This design groups the Critical + all 7 Important findings (plus the two previously-known pre-live
follow-ups) into structural workstreams, so each fix lands in a home that prevents the next
instance of its bug class. Cheap Minors that fall inside a workstream's blast radius are pulled
in; the rest are explicitly deferred (§10).

---

## 1. W1 — Stop lifecycle: `StopManager` (fixes Critical #1; absorbs known option-leg orphan-rest blocker)

**Problem.** EOD lifecycle at 16:15 calls `broker.cancel_all()` (`runner.py:115-120`), destroying
resting TRAILING_STOP protection while positions carry overnight. Nothing at `PRE_OPEN_SYNC` /
`ENTRY_OPEN` re-attaches stops — the only attachment sites are new BUY entries (`main.py:240`),
the §2.B unhedged fallback (`main.py:474`), and rebalance `consolidate_stop` (`main.py:657`,
default-off). From day 2 onward every carried position is unprotected; externally-signaled
positions have no exit at all except a future external SELL. (Moomoo orders default to DAY
time-in-force, so even without `cancel_all()` the stops would lapse overnight.)

**Design.** New module `autotrader/stops.py` exposing a `StopManager` whose single job is
reconciling **intended protection** vs **working orders**:

- `intended_protection(positions, db)` — every long position must have exactly one working
  trailing stop covering its full quantity (per-symbol; consolidated stops count).
- `reconcile(broker, db, risk, audit)` — runs at `PRE_OPEN_SYNC`:
  1. Fetch positions and open orders (via the W2 fail-loud query path).
  2. For each unprotected long, submit a trailing stop through the **existing audited path**
     (same trail bps, audit-before-place, risk-core routing as entry-time attachment).
  3. For each working stop with no matching position (orphan), cancel it.
  4. Log a one-line reconciliation summary (attached N, orphans cancelled M, already-covered K).
- **Option-leg orphan handling** (previously-known pre-live blocker): resting option-leg limits
  from `_route_overlay` with no surviving parent intent are detected in the same sweep and
  cancelled-or-escalated rather than left resting.

**Decision — keep `cancel_all()` at EOD.** DAY TIF makes overnight stops lapse anyway, and a
deterministic morning reconciliation also covers crash/restart and manually-placed orders.
Re-attach-at-open is the invariant; EOD cancellation becomes harmless. Rename the lifecycle step
(`EOD_FLATTEN` → `EOD_CANCEL_ORDERS`) so the name matches what it does (Minor #10a).

**Failure behavior.** If the open-orders query fails (W2: returns unknown), StopManager does
**not** attach (risk of duplicate stops) and does not cancel; it logs and retries on the next
lifecycle tick. If a stop submission is rejected, alert (Slack, reusing the UNHEDGED alert
plumbing) — an unprotected position is an operator-visible event.

**Tests.** Unprotected long gets stop re-attached at PRE_OPEN_SYNC; protected long untouched;
orphaned stop cancelled; orphaned option leg cancelled; query-failure tick is a no-op with retry;
rejection alerts. All against SimBroker, offline.

## 2. W2 — Broker queries fail loudly (fixes Important #1; house rule)

**Problem.** `get_open_orders()` returns `[]` on query failure (`moomoo_broker.py:244-245`) —
the same None-vs-empty class already fixed for `_positions()`. Downstream,
`_confirm_hedge_fill` (`main.py:451-456`) reports a hedge **confirmed FILLED** and
`_still_working` (`main.py:269-274`) reports a limit **filled** when the query merely rate-limited.
The trigger is real: `tick()` every 5 s costs ~24 refresh tokens/min vs the 20/min refill.

**Design.**

- **House rule:** every broker query method distinguishes *failed/limited* from *empty*. Adopt
  the `_positions()` pattern: return `None` on failure; never a default empty container.
  Applies to `get_open_orders()` and `reconcile_fills()`.
- **Conservative call sites:**
  - `_confirm_hedge_fill`: unknown → **not confirmed** (falls through to the existing §2.B
    fallback-stop + UNHEDGED alert after the bounded retries; never claims FILLED on unknown).
  - `_still_working`: unknown → **assume still working** (escalation neither abandons the order
    nor submits the next stage on unknown state; retries next dwell).
  - `StopManager.reconcile` (W1): unknown → no-op + retry.
- **Refresh-token budgeting:** `runner` fetches the account snapshot once per loop iteration and
  passes it into `tick()` / inbox routing instead of each path re-fetching. Halves sustained
  refresh usage and defuses the trigger for the failure mode above.

**Tests.** Broker stub returning `None`: hedge-confirm does not report FILLED; escalation holds
stage; StopManager no-ops. Token-budget test: one snapshot fetch per loop iteration.

## 3. W3 — Trading calendar (fixes Important #4)

**Problem.** `scheduler.py:37-44` gates on time-of-day + once-per-day only. On a Saturday the
entry gate opens, `tick()` evaluates Friday's close and can submit a BUY, rebalance fires at
12:30, and Slack receives weekend EOD reports. No weekday/holiday logic exists anywhere.

**Design.** Pure module `autotrader/market_calendar.py`:

- `is_trading_day(d: date, holidays: frozenset[date]) -> bool` — weekday < 5 and not a holiday.
- Holidays come from config (`RISK_MARKET_HOLIDAYS`, comma-separated ISO dates, default: 2026
  NYSE remainder), read via the existing frozen-config path — never hardcoded outside `config/`.
- The **scheduler** consults it once per loop off the injected NY-timezone clock and skips all
  jobs on non-trading days; the runner's tick loop is gated by the same check.
- Early-close (half-day) support is **out of scope** for this pass; noted in PRE-LIVE.md as a
  follow-up before holiday-season live operation.

**Tests.** Saturday/holiday: no jobs fire, tick is a no-op; Friday→Monday `_last_fired`
transition correct; config parse/validation of the holiday list.

## 4. W4 — Durable engine state (fixes Important #6; Minor #8 restart duplication)

**Problem.** Three pieces of state live only in process memory while a DB exists precisely for
projections: deferred pre-market BUYs (`main.py:104` — a restart between the pre-market drop and
09:45 silently loses the signal, the exact failure deferral was built to prevent), scheduler
`_last_fired` (restart after 16:30 re-fires EOD_REPORT → duplicate Slack post; can re-fire
REBALANCE), and there is no age limit on deferrals (a post-15:30 BUY replays ~18 h stale at the
next open).

**Design.**

- New DB table `engine_state` (key TEXT PRIMARY KEY, value TEXT/JSON, updated_at) behind proper
  `DB` methods — no `db._conn` reach-ins (extends the Minor #2 fix to new code).
- **Deferred entries:** persisted on defer, deleted on flush; each row stamped with
  `session_date`. On flush at ENTRY_OPEN, rows whose `session_date` != today are **expired** (logged
  + audit record, not routed) — same-day-only replay. Latest-wins dedupe per (symbol, direction)
  is preserved across restarts.
- **Scheduler:** `_last_fired` per job persisted through the same table; loaded at startup.
  Deliberate exception, preserved and commented: catch-up re-firing of `RISK_CHECK_*` after a
  mid-day restart is desirable halt-recovery behavior — only *at-most-once* jobs (EOD_REPORT,
  REBALANCE, EOD_CANCEL_ORDERS) read persisted state.

**Tests.** Defer → simulated restart (new engine on same DB) → flush routes the entry; stale
`session_date` expires with audit trail; EOD_REPORT does not re-fire after restart; RISK_CHECK
catch-up still fires.

## 5. W5 — Escalation dwell + cancel-race + touch-priced risk (fixes Important #3; known DESIGN follow-up)

**Problem.** `_submit_with_escalation` (`main.py:264-302`) has no dwell between stages — on live,
any limit not filled within one API round-trip is instantly cancelled (the feature degenerates to
"MARKET with extra calls"). Worse, when a cancel fails (commonly "already filled") the next stage
**submits anyway** → potential duplicate fill.

**Design.**

- **Injected dwell:** `escalation_dwell_seconds` config (default 20 s per stage), applied via
  the same injected-sleep pattern as `_confirm_hedge_fill` (`hedge_confirm_sleep`), so tests run
  instantly and live wiring passes `time.sleep`.
- **Cancel-race guard:** after any cancel failure, re-check `_still_working` (with W2 semantics)
  before submitting the next stage. Unknown or still-working → do **not** submit; filled → record
  terminal state and stop. Only a confirmed cancelled/absent order advances the stage.
- **Touch-priced MARKET risk check** (previously-known DESIGN follow-up): the risk evaluation for
  the terminal MARKET stage prices against the current touch/ask (from the snapshot already in
  hand per W2's budgeting) instead of `ref_price`, making the "risk vetoes MARKET fallback"
  defensive branch reachable — and testable (restores the intent of the dropped T6b test).

**Tests.** Dwell respected (injected sleep called with configured value); cancel-failure +
still-working → no second submit; cancel-failure + filled → terminal recorded once; MARKET stage
vetoed when touch-priced notional breaches the limit (T6b resurrected against real config).

## 6. W6 — Options exits exempt from entry budget (fixes Important #5)

**Problem.** `risk_core.py:176-185`'s else-branch applies the NLV premium budget to every
non-(SELL+OPEN) leg — including BUY-to-CLOSE. Buying back a short call that moved against you can
cost more than the entry budget, so the risk core would block the exit ("always able to flatten"
violated). Latent until the O4 close-scan lands, but the path exists now.

**Design.** Exempt `position_effect == "CLOSE"` legs from the premium budget and absolute
ceiling, mirroring the equity reduce-only exemption (`risk_core.py:83-115`); CLOSE legs still pass
all structural checks (audit, stale-data refusal, notional sanity). Add the exemption rationale
as a comment referencing the equity precedent.

**Tests.** BUY+CLOSE above budget passes; BUY+OPEN above budget still vetoed; CLOSE with stale
snapshot still refused.

## 7. W7 — Single writer for the daily performance row (fixes Important #7)

**Problem.** `runner._record_perf` (`runner.py:80-92`) computes fills-derived realized P&L and
quote-based unrealized — the report-integrity semantics. But `TradeEngine._route_signal`
(`main.py:177-184`) and `apply_risk_check` (`main.py:684-688`) also write the row using raw broker
`snap.day_pnl` / `snap.unrealized_pnl`. A stop SELL routed between 16:15 and 16:30 (SELLs are
never gated) overwrites the authoritative EOD numbers with figures the report-integrity plan
explicitly distrusts.

**Design.** The engine stops writing `performance` entirely; `runner` (EOD write + risk sweeps)
becomes the **sole writer**, always using the fills-derived computation. Engine paths that
currently write it keep their other recording (signals, trades, audit) unchanged.

**Tests.** Post-EOD stop SELL no longer mutates the performance row; EOD report reads the
runner-computed values; risk-sweep write unchanged.

## 8. W8 — Wiring fix + PRE-LIVE.md gate (fixes Important #2; codifies the checklist)

- **One-line fix:** pass `hedge_confirm_sleep=time.sleep` in the `TradeEngine` construction at
  `main.py:769-771`, so the bounded hedge fill-poll actually waits on live instead of running
  three instantaneous checks (spurious UNHEDGED alerts / unnecessary fallback stops). Test:
  engine built by `main()` has a real sleep, not the no-op default.
- **`PRE-LIVE.md`** at repo root: the mechanical gate for flipping `RISK_TRADING_ENV=LIVE` /
  `RISK_LIMIT_ORDERS_ENABLED` / `RISK_REBALANCE_ENABLED`. Contents: one checkbox per workstream
  W1–W7 (with the finding it closes), the deferred items (§10) marked non-blocking, the early-close
  calendar follow-up, and the pre-existing human live-session exit gate. Rule recorded in the doc:
  **no live flag flips while any checkbox is open.** Also fix the CLAUDE.md/config naming mismatch
  it would otherwise memorialize (`TRADING_ENV` vs `RISK_TRADING_ENV`, Minor #10b) by correcting
  CLAUDE.md.

## 9. Sequencing, testing, error handling

**Order:** W1 (critical, paper-affecting today) → W2 (W1's reconcile depends on fail-loud
queries — implement the W2 broker change first if interleaving) → W8a (one-line sleep wiring,
can land any time) → W3 + W4 (independent of each other) → W5 → W6 → W7 → W8b (PRE-LIVE.md last,
listing what actually landed).

Practical note: W1's design depends on W2's query semantics; the implementation plan should land
the `get_open_orders()` fail-loud change before or together with `StopManager.reconcile`.

**Testing discipline:** TDD throughout (project convention); every workstream's tests run
offline against SimBroker/stubs — no OpenD required; full suite green before each commit; no
regressions to the current 519 collected tests.

**Error-handling posture (uniform):** unknown broker state is never treated as success or
absence; every skipped/expired/vetoed action leaves an audit record; operator-visible failures
(unprotected position, rejected stop) alert via the existing Slack plumbing; all new intervals,
budgets, holidays, and flags live in config — nothing hardcoded.

## 10. Explicitly deferred (tracked in PRE-LIVE.md as non-blocking)

From the review's Minor list, deferred to a later cleanup pass: SimBroker `total_assets`
ignoring position market value (#3); overlay `limit_price` recorded for MARKET legs (#4);
gated-overlay signal-row spam (#5); escalation intermediate orders absent from the trades
projection (#6); planner stock anchor always MARKET — document-or-change (#7); UNKNOWN-ack
reconciliation job (#9); machine-local dates in `db._today()` / `runner._compute_realized` (#1 —
new W3/W4 code uses the injected NY clock, but migrating existing call sites is deferred);
broader `db._conn` reach-in cleanup in `EODReporter._gather` (#2 — new code uses DB methods).

Out of scope entirely: the N-day breakout strategy (separate spec, 0f00e05), early-close calendar
support, and the uncommitted signals coerce/normalize WIP in the working tree.
