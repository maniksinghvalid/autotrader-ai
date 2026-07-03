# PRE-LIVE Gate — Staged (v2)

**Rule: no live flag flip (`RISK_TRADING_ENV=LIVE`) while ANY Stage-1 box below
is open.** Stage 2 and Stage 3 gate their own additional flags — see the stage
table. Source: `docs/superpowers/specs/2026-07-02-live-readiness-v2-design.md`
(the "Live-Readiness v2" spec, workstreams V1–V11) and the 22-task
implementation plan that closed it out. Companion document: `CUTOVER.md` (the
step-by-step procedure for actually flipping the flags once every box here is
checked).

> **Superseded:** the original flat W1–W8 checklist from the 2026-07-02
> `pre-live-hardening-design.md` spec is fully absorbed into this staged gate
> (its W1–W8 workstreams map onto V1–V5/V8 below) and is no longer tracked
> separately. If you are looking for "W1 StopManager re-attach" or similar,
> see V3/V11 below — the item is done and now expressed as a V-numbered,
> test-backed box instead of a standalone W-item.

---

## Stage model

| Stage | State | Flags | Gated by |
|---|---|---|---|
| **0** (today) | Paper, everything enabled | unchanged; `RISK_ACCOUNT_OWNERSHIP=SHARED` while the SNP bot coexists on the paper account | V11 |
| **1** (first live flip) | Internal breakout strategy + trailing stops + risk halts only | `RISK_TRADING_ENV=LIVE`; `RISK_ACCOUNT_OWNERSHIP=SOLE` (enforced at config-load); `RISK_SIGNALS_ENABLED=0`; `RISK_LIMIT_ORDERS_ENABLED=0`; overlays off (`RISK_ALLOWED_OVERLAYS` empty); `RISK_REBALANCE_ENABLED=0` | V1–V5, V8, V9, V10, V11 (all boxes below) |
| **2** | Webhook/inbox signals enabled | `RISK_SIGNALS_ENABLED=1` | V6 boxes + 5 clean Stage-1 sessions (`CUTOVER.md` §6) |
| **3** | Limit-order escalation, options overlays, rebalancing | `RISK_LIMIT_ORDERS_ENABLED=1`; overlays on; `RISK_REBALANCE_ENABLED=1` | V7 boxes (+ V5's Stage-3 exposure) + 5 more clean Stage-2 sessions + a logged rebalance dry-run |

Promotion criteria ("clean session," dry-run procedure) live in `CUTOVER.md`
§6 — this file only tracks whether the underlying code/ops boxes are checked.

---

## Stage 1 — code (V1–V5, V8–V11)

- [x] **V1 — Live-shaped sim rig.** `SimBroker` gained an async-fill mode
      (`fill_latency_ticks`/`cancel_latency_ticks` + `tick_market()`) so
      live-only races (async fills, cancel-vs-fill) are testable offline.
      Artifact: `tests/test_sim_broker_async.py`.
- [x] **V2 — Scheduler integrity (Critical C1).** `LifecycleScheduler.poll()`
      is read-only; `mark_fired(name, now)` only marks a job done after it
      succeeds. The runner wraps each due job in its own try/except and
      alerts on failure (`job-fail:{job}:{date}`) without blocking sibling
      jobs in the same batch. Artifact: `tests/test_scheduler_mark_on_success.py`,
      `tests/test_scheduler.py`.
- [x] **V3 — Live-safe stop attachment (Critical C2).** `_attach_trailing_stop`
      confirms the entry actually filled (`_confirm_off_book`, bounded poll)
      before attaching, instead of trusting an immediate post-submit
      snapshot. `StopManager.reconcile` also runs at `RISK_CHECK_MID`,
      `RISK_CHECK_LATE`, and `RISK_SWEEP` (not just `ENTRY_OPEN`) as an
      intraday backstop, skipped only while the gate is halted. Artifact:
      `tests/test_stop_attach_async.py`, `tests/test_intraday_stop_reconcile.py`,
      `tests/test_stop_manager.py`.
- [x] **V4 — Risk-halt correctness.** `AccountSnapshot.day_pnl_known` fails
      closed (GATE, not a silent zero) when broker P&L is absent/NaN/garbage.
      `RISK_UNREALIZED_LOSS_GATE` blocks new entries only on unrealized
      drawdown — never auto-flattens; the hard HALT stays realized-only. A
      hard HALT persists to `engine_state` (`halt:<date>`) and is restored on
      restart (`lifecycle.restore_session_halt`). HALT cancels-all **before**
      flattening (was flatten-then-cancel), so liquidation SELLs can't be
      swept by the immediately-following cancel. Artifact:
      `tests/test_day_pnl_fail_closed.py`, `tests/test_durable_halt.py`,
      `tests/test_halt_flatten_order.py`, `tests/test_unrealized_gate.py`,
      `tests/test_unrealized_snapshot.py`.
      **Known gap (fail-open, not fail-closed):** unlike `day_pnl_known`'s
      fail-closed guarantee for the realized-P&L HALT, `unrealized_pnl` has
      no equivalent `unrealized_pnl_known` flag — it comes from
      `safe_float(..., default=0.0)` over the broker's `unrealized_pl` field.
      If `RISK_UNREALIZED_LOSS_GATE` is enabled and the broker ever stops
      reporting `unrealized_pl` (an API-shape-change scenario), the computed
      `unrealized_pnl` silently becomes `0.0` and the gate never trips — it
      fails open (silently stops protecting) rather than blocking entries.
      Bounded impact: this only affects the optional, default-off,
      entry-blocking-only unrealized gate; the primary realized-loss HALT
      is unaffected and still fails closed. No code change is planned here;
      this is a documented pre-live awareness item.
- [x] **V5 — Order-status & escalation safety (Stage-1 code; full exposure at
      Stage 3).** Escalation's cancel step confirms the order left the book
      (bounded poll) before advancing — `RET_OK` on a cancel request no
      longer means "gone." Unknown/unmapped broker statuses count as
      "working," never silently dropped as filled. `load_risk_config()`
      rejects `RISK_LIMIT_ORDERS_ENABLED=1` with both order caps at 0.
      Artifact: `tests/test_escalation_cancel_confirm.py`,
      `tests/test_unknown_status_conservative.py`, `tests/test_config.py`.
- [x] **V8 — Supervision & alerting (Critical C5).** `autotrader/alerts.py`
      `AlertSink` posts best-effort, once-per-episode-deduped Slack alerts,
      never raises; wired to scheduler job failures, hard HALT, watchdog-
      unhealthy episodes (also recorded in the `halts` table, audit-only —
      never gates a future restart), and 5+ consecutive loop errors.
      `EOD_CANCEL_ORDERS` syncs fills before recording the day's final
      performance row. launchd supervision (`deploy/*.plist`), rotating file
      logging (`AUTOTRADER_LOG_DIR`), and a dead-man heartbeat
      (`AUTOTRADER_HEARTBEAT_URL`) exist. Artifact: `tests/test_alerts.py`,
      `tests/test_operational_alerts.py`, `tests/test_heartbeat.py`,
      `deploy/com.autotrader.trader.plist`, `deploy/backup_autotrader.sh`.
- [x] **V9 — Config & hygiene.** `RISK_SIGNALS_ENABLED` (the Stage-1 cutover
      flag) exists and defaults to `true` (paper-preserving). `BreakoutParams`
      (stop-loss/take-profit/confidence) and the audit-journal path
      (`AUTOTRADER_AUDIT_PATH`, new default `~/.autotrader_trade_audit.jsonl`)
      are config-driven, not hardcoded. NYSE holiday calendar covers 2026 AND
      2027; `holiday_horizon_warning()` logs when the calendar nears expiry.
      Artifact: `tests/test_config_v9.py`, `tests/test_market_calendar.py`.
- [x] **V10 — Process, docs & cutover.** CI (`.github/workflows/tests.yml`)
      runs the offline suite on push/PR. This document, `CUTOVER.md`, and
      `RUNBOOK.md` are truth-passed against the current code (this task).
      Artifact: `.github/workflows/tests.yml`, this file, `CUTOVER.md`,
      `RUNBOOK.md`.
- [x] **V11 — SNP-bot coexistence (Critical C6).** `RISK_ACCOUNT_OWNERSHIP`
      (`SOLE` default | `SHARED`) exists; `RISK_TRADING_ENV=LIVE` +
      `RISK_ACCOUNT_OWNERSHIP=SHARED` is refused at config-load time — live
      money never runs scoped-down. In `SHARED`, `StopManager.reconcile`'s
      orphan sweep and attach pass, `TradeEngine.cancel_working_orders()`
      (via `cancel_tracked_orders`), `_flatten_all`, and
      `ground_truth_sync(owned_only=True)` all scope to AutoTrader's own
      tracked book (`db.owned_symbols()`) — the SNP bot's orders/positions/
      fills are never touched. `SOLE` (the default, and the mode Stage 1
      actually runs live in) is verified byte-for-byte identical to
      pre-V11 behavior. Artifact: `tests/test_account_ownership_config.py`,
      `tests/test_shared_ownership_scoping.py`.

## Stage 1 — operational

- [ ] **Supervision reboot test.** Reboot the host running the trader and
      OpenD; confirm (without manually starting anything) `launchctl list |
      grep com.autotrader` shows the trader (and webhook, if installed)
      running with a fresh PID, and its stdout log shows a fresh startup
      banner. See `deploy/README.md` §6 checklist item 2.
- [ ] **Backup restore drill.** Run `deploy/backup_autotrader.sh` (or wait
      for its scheduled 17:30 firing), then open the resulting
      `~/.autotrader_backups/autotrader-<date>.db` with `sqlite3 ... ".tables"`
      and confirm it opens cleanly with the expected tables populated — not
      just that a file exists. See `deploy/README.md` §6 checklist item 4.
- [ ] **Alert live-fire.** Force a real HALT (or a scheduler job failure) on
      paper and confirm the Slack message actually lands, not just that
      `AlertSink.send` was called in a test. See `CUTOVER.md` §1.
- [ ] **Holiday horizon.** Confirm `RISK_MARKET_HOLIDAYS` (or the shipped
      default in `autotrader/config.py`) extends comfortably past the next
      live session and that `holiday_horizon_warning()` is not currently
      firing. Early-close (half-day) calendar support is **not** covered by
      this box — see the deferred list below; it is required before any
      holiday-season live session specifically.
- [ ] **RUNBOOK refreshed.** `RUNBOOK.md` matches the current code (test
      count, 8-job lifecycle table, stop-reconcile behavior, full config
      knob list, launchd section, no real ngrok hostname) — done as part of
      this task; re-check this box if `RUNBOOK.md` is edited again before
      cutover without a fresh truth-pass.
- [ ] The paper-only guards are consciously revised for the live cutover per
      `CUTOVER.md` §2/§3: `main()` refuses `trading_env != "PAPER"`
      (`autotrader/main.py`) and the risk core rejects non-PAPER envs
      (`autotrader/risk_core.py`). For this plan's scope this is a
      **config** change (`RISK_TRADING_ENV=LIVE`), not a code edit — confirm
      `RISK_ACCOUNT_OWNERSHIP=SOLE` is asserted by config (LIVE+SHARED
      structurally refuses to start).
- [ ] Trade password unlocked **manually** in the OpenD GUI (never via SDK).
- [ ] Human live-session exit gate (pre-existing, from Phase 1/2): a human
      is present and able to intervene for the entirety of the first live
      session(s).

## Stage 2 — code (V6)

- [ ] **V6 — Ingress security (Critical C3).** Webhook auth compares bytes
      (non-ASCII headers -> clean 401, not a 500); an independent bearer
      token (`AUTOTRADER_WEBHOOK_TOKEN`) is supported separate from the HMAC
      signing secret; 401 audit writes are truncated/size-capped/throttled.
      The webhook rejects payloads outside a freshness window
      (`AUTOTRADER_WEBHOOK_FRESHNESS_MIN`, default 15 min, past and future).
      `SignalInbox` dedupes by `routine_id` and quarantines payloads older
      than `AUTOTRADER_SIGNAL_TTL_HOURS` (default 24h) — defense-in-depth
      since the file-drop adapter bypasses the webhook. Inbox/webhook
      filenames prefix a nanosecond timestamp so processing order equals
      arrival order. A failing targets-sink no longer blocks that payload's
      signals or later files. Artifact: `tests/test_webhook.py`,
      `tests/test_replay_protection.py`, `tests/test_signal_inbox.py`,
      `tests/test_inbox_ordering_targets.py`,
      `tests/test_signal_schema_targets.py`.
- [ ] Webhook secret rotation has been exercised at least once (rotate
      `AUTOTRADER_WEBHOOK_SECRET`/`AUTOTRADER_WEBHOOK_TOKEN` in
      `config/secure.config`, restart, confirm the old value is rejected and
      the new one accepted).

## Stage 3 — code (V7, V5 full exposure)

- [ ] **V7 — Options chain safety (Critical C4).** Option contract selection
      requires a finite, nonzero delta — a chain with missing/NaN/zero-delta
      rows returns no contract, never the "closest" garbage row. COLLAR
      structure validation enforces put-strike < call-strike (previously
      exempt). Artifact: `tests/test_chain_delta_safety.py`,
      `tests/test_chain_windows.py`, `tests/test_options_chain.py`,
      `tests/test_overlays_registry.py`.
- [ ] **V5 full exposure.** Limit-order escalation (dwell + cancel-confirm +
      unknown-status handling, code-complete at Stage 1 — see above) is now
      actually live-exercised for the first time; re-confirm the escalation
      log sequence on a real order in the first Stage-3 session before
      trusting it unattended.
- [ ] A logged rebalance dry-run has been performed and reviewed per
      `CUTOVER.md` §6 (`compute_plan` invoked directly, `RISK_REBALANCE_ENABLED`
      still `0`, zero orders submitted).

---

## Non-blocking follow-ups (deferred; per spec §6)

- Early-close (half-day) calendar support — required before holiday-season
  live, tracked separately from the general holiday-horizon box above.
- `AccountSnapshot.gross_exposure()` cost-basis vs. market-value drift
  (SimBroker `total_assets` ignores position market value).
- Overlay legs record `limit_price` for MARKET orders.
- Gated-overlay signal-row spam.
- Escalation intermediate orders absent from the trades projection.
- Dead `opens_stock` stock-anchor path in overlays/planner — remove or
  comment in a later pass (planner stock anchor is always MARKET).
- UNKNOWN-ack reconciliation job.
- Machine-local dates in `db._today()` / `runner._compute_realized`.
- `db._conn` reach-ins in `EODReporter._gather` / runner.
- Watchdog active re-connect (today it only polls and relies on SDK
  auto-reconnect).
- Slack 50-block cap in `EODReporter._build_blocks`.
- `coerce._coerce_change` changed-vs-direction check ordering;
  `routine_adapter` non-numeric `composite_score` crash granularity.
- Cross-repo golden-fixture contract test for portfolio targets (requires
  the producer repo, ai-trading-claude; tracked there).
- `RateLimiter.acquire` silent 60s block logging; `trades.state` projection
  staleness; `resolve_halt` dead code.
