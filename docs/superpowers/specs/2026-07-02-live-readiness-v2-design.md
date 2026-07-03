# Live-Readiness v2 — Staged Cutover Hardening

**Date:** 2026-07-02
**Status:** Approved (brainstorming session, post-merge three-lens review)
**Supersedes:** the flat checklist in `PRE-LIVE.md` (rewritten by V10 as a staged gate)
**Provenance:** three parallel scoped reviews of `main..develop` (6135b89..377a178, 587 tests
passing) conducted 2026-07-02 after the W1–W8 pre-live hardening round and the breakout-entry
slice merged. Reviews covered (1) the core order/risk path, (2) signal ingress + options +
rebalance, (3) operations/reporting/process. All three returned "not ready for live."

## 1. Problem

The paper-v1 system is architecturally sound (privilege separation, single risk gate,
audit-before-order, fail-loud broker contracts — all confirmed under review), but the reviews
found five Critical gaps and ~20 Important ones that the earlier piecewise task reviews missed:

- **C1** — `LifecycleScheduler.poll` marks jobs fired (and durably persists AT_MOST_ONCE state)
  *before* any job runs, and one exception aborts the remaining due jobs. A single broker
  timeout during `PRE_OPEN_SYNC` silently consumes `ENTRY_OPEN` for the day: no stop
  re-attach, no deferred-entry flush; a failed `EOD_CANCEL_ORDERS` never retries.
- **C2** — `_attach_trailing_stop` re-fetches the snapshot immediately after the entry. On live,
  MARKET BUYs ack `SUBMITTED` and fill asynchronously, so the stop systematically fails to
  attach, and `StopManager.reconcile` runs only at the next morning's `ENTRY_OPEN`. Every
  intraday live entry runs unprotected until the following session.
- **C3** — The signal ingress has no replay protection: HMAC covers the body only; no timestamp
  freshness window, no `routine_id` dedup at the webhook, inbox, or consumer. A captured
  request is valid forever and produces real duplicate orders.
- **C4** — Missing/NaN option greeks pass through the broker boundary (`delta` defaults to 0;
  NaN survives `safe_float`). An all-zero-delta chain makes the covered-call planner select the
  deepest ITM call by tie-break and sell it at market. COLLAR is exempt from structure
  validation.
- **C5** — No supervision or real-time alerting. The documented runtime is hand-launched
  terminals; nothing restarts the trader after crash/reboot/sleep, and the two worst events
  (daily-loss HALT that flattens the book; `HALTED_UNHEALTHY` watchdog exhaustion) post
  nothing to Slack. The operator learns of a 13:30 flatten from the 16:30 EOD report at best.

Plus, condensed: a realized-only daily-loss check that fails *open* when broker fields are
missing; a session halt that does not survive restart; a halt-flatten that can cancel its own
liquidation orders; an escalation cancel-race (`RET_OK` means request-accepted, not cancelled);
unknown order statuses treated as off-book successes; hardcoded `BreakoutParams` risk values in
`main()`; ingress robustness holes (non-ASCII header 500, unbounded 401 audit writes, random
inbox processing order, future-dated targets wedging the rebalancer, sqlite errors wedging all
signal flow, token == HMAC key); and process debt (no CI, RUNBOOK drift, undocumented knobs,
no backups, no written cutover procedure, untracked-but-referenced files).

## 2. Goal & success criteria

Close every gap above in **one spec → one phased implementation plan**, so live cutover can
proceed in stages. Success means:

1. Every Critical has a regression test that fails against today's code and passes after the fix.
2. Full offline suite stays green (587+ tests, 0 unexpected skips).
3. `PRE-LIVE.md` v2 is a staged gate whose Stage-1 boxes can all be checked.
4. A written, rehearsable `CUTOVER.md` (procedure + rollback) exists.
5. The trader survives a host reboot unattended and alerts on its own death or halt within
   minutes, not at end of day.

## 3. Cutover stage model

Staging governs **flag flips only**. All code fixes in this spec land now, in one plan.

| Stage | State | Flags | Gated by |
|-------|-------|-------|----------|
| 0 | Paper, everything enabled (today) | unchanged | — |
| 1 | **First live flip**: internal breakout strategy + trailing stops + risk halts only | `RISK_TRADING_ENV=LIVE`; **new** `RISK_SIGNALS_ENABLED=0`; `RISK_LIMIT_ORDERS_ENABLED=0`; overlays off; `RISK_REBALANCE_ENABLED=0` | V1–V5, V8, V9, V10 |
| 2 | Webhook/inbox signals enabled | `RISK_SIGNALS_ENABLED=1` | V6 + N clean Stage-1 sessions |
| 3 | Limit-order escalation, options overlays, rebalancing | `RISK_LIMIT_ORDERS_ENABLED=1`; overlays on; `RISK_REBALANCE_ENABLED=1` | V7 (+ V5 exposure) + N clean Stage-2 sessions + logged rebalance dry-run |

`RISK_SIGNALS_ENABLED` is a new config flag (default **on**, preserving paper behavior) that
gates the inbox poll and `submit_external_signal`. Stage promotion criteria ("N clean sessions"
and what "clean" means) are defined in `CUTOVER.md` (V10).

Rebalance note: `compute_plan` intentionally does not cap reduce-only TRIM sizes (exits skip
`max_order_notional` by design), so first enablement against a concentrated book can emit large
single MARKET sells. Stage 3 therefore requires a dry run with execution stubbed and the plan
logged for human review before the flag flips.

## 4. Workstreams

### V1 — Live-shaped sim rig *(foundation; Stage 1)*

Add an **async-fill mode** to `SimBroker` (`autotrader/sim_broker.py`):

- `place_order` acks `SUBMITTED`; the fill materializes on a later `tick()`/clock advance
  (configurable latency in ticks).
- `cancel_order` acks a cancel *request*; the cancel takes effect after a configurable latency,
  during which a racing fill can win (deterministically scriptable per test).
- Synchronous behavior remains the default; existing tests are untouched.

V2–V5 regression tests run against this rig — it converts the live-only findings (C2, cancel
races, halt-flatten self-cancel) into executable offline tests and protects future changes.

### V2 — Scheduler integrity *(C1; Stage 1)*

In `autotrader/scheduler.py` + `autotrader/runner.py`:

- Each due job executes inside its own try/except; one failure never consumes the batch.
- `_last_fired` and durable AT_MOST_ONCE state are written **only after the job returns
  successfully**; a failed job re-fires on the next poll.
- Job failure posts a Slack alert (V8 plumbing) and logs with `exc_info=True`.
- Tests: job raises → later due jobs still run; failed job retries next poll; no double-fire
  after success; AT_MOST_ONCE state not persisted on failure.

### V3 — Live-safe stop attachment *(C2; Stage 1)*

Two layers in `autotrader/main.py` + `autotrader/runner.py`:

1. `_attach_trailing_stop` attaches off the **reconciled fill**: poll open-orders/fills for the
   entry cid (same pattern as `_confirm_hedge_fill`) rather than trusting the immediate
   post-entry snapshot.
2. Backstop: `StopManager.reconcile` also runs at `RISK_CHECK_MID`, `RISK_CHECK_LATE`, and
   `RISK_SWEEP` — an unprotected position is caught within minutes, not the next morning.

Tested against V1's async fills (entry acks SUBMITTED → fills later → stop attaches from the
fill; orphan injected mid-day → intraday reconcile attaches it).

### V4 — Risk-halt correctness *(Stage 1)*

In `autotrader/moomoo_broker.py`, `autotrader/risk_check.py`, `autotrader/main.py`,
`autotrader/db.py`:

- **Fail closed:** if neither `realized_pl` nor `today_pnl_value` is present in the account
  snapshot, `day_pnl` is unknown → snapshot is stale → entries blocked. Never default to 0.
- **Unrealized entry block (new, approved):** a second configurable threshold on unrealized
  drawdown that *blocks new entries only* — it never auto-flattens. The hard HALT remains
  realized-only.
- **Durable halt:** a hard HALT persists to `engine_state` keyed by session date and is
  restored in `main()` on startup; a crash/restart cannot silently re-open the gate.
- **Flatten ordering:** on hard HALT, `cancel_all` runs **before** `_flatten_all`, and the
  flatten cids are exempt from any subsequent cancel sweep — liquidation SELLs cannot cancel
  themselves.

### V5 — Order-status & escalation safety *(Stage 1 code; Stage 3 exposure)*

In `autotrader/main.py`, `autotrader/moomoo_broker.py`, `autotrader/config.py`:

- Escalation verifies terminal `CANCELLED` status (polling, like `_order_working`) before
  submitting the next stage; `RET_OK` from a cancel request is never treated as cancelled.
- Unknown broker order statuses map to a conservative UNKNOWN/working bucket — never silently
  dropped as complete (CLAUDE.md: unrecognized statuses are not successes).
- `load_risk_config` rejects `limit_orders_enabled=True` when both order caps are zero.

### V6 — Ingress security *(C3 + Importants; Stage 2 exposure, fixed now)*

In `autotrader/signals/webhook.py`, `inbox.py`, `main.py`, `db.py`, `config/`:

- **Replay defense in depth:** (a) the webhook rejects payloads whose `timestamp` is outside a
  configurable tz-aware freshness window (default ±15 min); (b) the consumer records processed
  `routine_id`s in the DB and skips duplicates idempotently — this also covers the file-drop
  adapter path that bypasses the webhook.
- **Two independent secrets:** bearer token and HMAC signing key are separate values in
  `secure.config`; a header leak alone no longer grants forgery. Rotation documented in the
  RUNBOOK.
- **Auth robustness:** header comparison in bytes (`.encode("utf-8", "replace")`) — non-ASCII
  headers yield an audited 401, never an unaudited 500.
- **Bounded audit:** 401 audit records cap attacker-controlled field lengths; the audit file
  has a size bound/rotation; 401 writes are throttled per remote address.
- **Ordered inbox:** signal files named `<epoch-ns>-<uuid>.json` by both writers so processing
  order equals arrival order (superseded BUY can no longer execute after its correcting SELL).
- **Targets safety:** `as_of_date > today` is clamped/rejected at ingest (a future-dated payload
  can no longer permanently wedge the rebalancer); the `on_targets` callback catches broad
  `Exception` and quarantines the file so a sqlite error cannot block all signal flow.
- **Signal TTL:** payloads older than a configurable age are quarantined, not routed (mirrors
  the same-day expiry already built for deferred entries).

### V7 — Options chain safety *(C4; Stage 3 exposure, fixed now)*

In `autotrader/moomoo_broker.py`, `autotrader/options/chain.py`, `planner.py`:

- Broker boundary drops chain rows whose delta is absent, NaN, or zero — degraded greeks
  produce `SKIP_NO_CONTRACT`, never garbage selection.
- `select_contract` independently requires `math.isfinite(q.delta) and q.delta != 0`.
- COLLAR loses its `_validate_structure` exemption: put strike must be below call strike.
- Tests: all-zero-delta chain → skip; single-NaN chain → NaN row never wins regardless of
  order; inverted collar → rejected.

### V8 — Supervision & alerting *(C5; Stage 1)*

- **launchd KeepAlive agents** for the trader and the webhook: restart on crash and on reboot;
  logs to rotated files under `~/Library/Logs/autotrader/` (replacing stdout-only logging);
  power/no-sleep settings documented for this Mac as the production host.
- **Slack alerts** (reusing the existing `_post_slack`/`alert_url` plumbing) for: hard HALT
  (flatten event), `HALTED_UNHEALTHY` (alert once per episode, not per loop), scheduler job
  failure (V2), and N consecutive `run_once` errors (throttled; log with `exc_info=True`).
- **Dead-man heartbeat:** a periodic ping to an external monitor (healthchecks.io or
  equivalent) from the run loop, so silence itself alarms.
- **EOD integrity:** `EOD_CANCEL_ORDERS` syncs fills before the 16:15 performance row, so
  15:30–16:30 fills (e.g. a late trailing-stop trigger) appear in the day's perf row and the
  16:30 report.
- **Post-mortem trail:** watchdog-unhealthy episodes are recorded in the `halts` table.

### V9 — Config & hygiene *(Stage 1)*

- `BreakoutParams` (stop-loss pct, take-profit pct, confidence) and the audit-journal path move
  from hardcoded `main()` values to config (CLAUDE.md hard rule).
- `RISK_MARKET_HOLIDAYS` default extended through 2027+; startup warning when the calendar
  horizon is within 60 days of expiry (the 2026-only silent degradation cannot recur).
- Every operator-visible knob — `RISK_SIGNALS_ENABLED`, holidays, `RISK_ORDER_CAP_BPS/TICKS`,
  `RISK_ESCALATION_DWELL_SECONDS`, `RISK_LIMIT_ORDERS_ENABLED`, snapshot-cache ticks, the new
  unrealized threshold, webhook freshness window/TTL — documented in **both**
  `config/risk.config.example` and the RUNBOOK.

### V10 — Process, docs & cutover *(Stage 1)*

- **CI:** minimal GitHub Actions workflow — offline pytest on push/PR to `develop`
  (live-marked tests excluded).
- **RUNBOOK truth-pass:** 587-test count; all 8 scheduler jobs with current names
  (`EOD_CANCEL_ORDERS`, `REBALANCE`, `RISK_CHECK_MID/LATE`, `EOD_REPORT`, …); StopManager
  re-attach/orphan-sweep behavior replacing the stale §7 caveat; launchd operations section;
  placeholder hostname instead of the real ngrok URL.
- **Backups:** nightly `VACUUM INTO` dated copy of `~/.autotrader.db` + copy of
  `~/.futu_trade_audit.jsonl` via a launchd timer; retention and restore drill documented.
- **Working-tree hygiene:** commit `docs/research/` and `docs/routinesignal-ticker.json`
  (referenced by committed docs) and `uv.lock`; gitignore `plan/`, `state.md`, scratch JSON.
- **Branch policy:** `develop` is the integration branch; `main` fast-forwards from `develop`
  at each cutover stage.
- **`CUTOVER.md`:** the exact two-file paper-guard revision (`autotrader/main.py` refusal +
  `autotrader/risk_core.py` env check — the only sanctioned edit path, with human review);
  first-session reduced caps; kill-switch test (verify Ctrl-C → cancel-all works live);
  rollback steps; per-stage promotion criteria (N clean sessions, definition of "clean").
- **`PRE-LIVE.md` v2:** rewritten as the staged gate referencing V1–V10, replacing the flat
  checklist.

## 5. Testing strategy

- Every Critical gets a regression test that **fails on today's code**: C1 (job raises → batch
  survives → retry next poll), C2 (async fill → stop attaches from fill; orphan → intraday
  reconcile), C3 (replayed body → second enqueue/route is a no-op; stale timestamp → 401),
  C4 (zero/NaN-delta chains → skip; inverted collar → rejected), C5 code-half (HALT posts to a
  stubbed Slack).
- V1's async rig is the enabling mechanism for C2/V5 offline coverage.
- Ops items that cannot be unit-tested (launchd restart, backups) get explicit verification
  steps in the implementation plan (reboot "pull-the-plug" test; restore-from-backup drill)
  and corresponding checkboxes in PRE-LIVE v2.
- The full offline suite must remain green throughout; live-path tests stay opt-in.

## 6. Out of scope (tracked, deferred)

- The 9 pre-existing PRE-LIVE.md non-blocking minors (early-close calendar remains required
  before holiday-season live).
- Watchdog active re-connect (today it only polls and relies on SDK auto-reconnect).
- `AccountSnapshot.gross_exposure()` cost-basis vs market-value drift.
- Dead `opens_stock` stock-anchor path in overlays/planner (remove or comment in a later pass).
- Slack 50-block cap in `EODReporter._build_blocks`.
- `coerce._coerce_change` changed-vs-direction check ordering; `routine_adapter` non-numeric
  `composite_score` crash granularity.
- Cross-repo golden-fixture contract test for portfolio targets (requires the producer repo,
  ai-trading-claude; noted there).
- `RateLimiter.acquire` silent 60s block logging; `trades.state` projection staleness;
  `resolve_halt` dead code (subsumed if V4's durable-halt work touches it).

## 7. Execution shape

One implementation plan (~18–22 TDD tasks), executed subagent-driven on a feature branch off
`develop`, mirroring the W1–W8 round. Dependency order: V1 first (rig), then V2–V5 (core), then
V6/V7 (parallelizable), then V8–V10 (ops/docs; V8's alert plumbing lands before V2's alert
call site or is stubbed until then — plan resolves this ordering).
