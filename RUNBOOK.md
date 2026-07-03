# AutoTrader — Operations Runbook

How to run the AutoTrader paper-trading system: the continuous trading loop, the read-only dashboard, external signal ingress, and trailing stops. Current as of **Phase 2c** (paper-v1).

> **Safety first.** This system is **paper-only** (`TrdEnv.SIMULATE`). `main()` refuses to start unless `RISK_TRADING_ENV=PAPER`. It **never** sends an SDK trade-unlock — unlocking is a manual action in the OpenD GUI. Every order flows through the deterministic risk core and is written to an audit log *before* it is placed. Do not try to bypass these.

---

## 1. What actually runs

There are **two independent processes**, each talking to a local **Moomoo OpenD GUI** on `127.0.0.1:11111`:

| Process | Command | Role |
|---|---|---|
| **Trader** | `python3 -m autotrader.main` | The continuous, self-healing trading loop (lifecycle scheduler + watchdog + engine). Places/cancels orders. |
| **Dashboard** | `python3 dashboard/server.py` | Read-only web view of the paper account (summary, positions, open orders, fills). **Never** places orders. |

Both require OpenD running and authenticated. The dashboard is optional but recommended for monitoring.

---

## 2. Prerequisites (one-time)

1. **Moomoo OpenD GUI** (the desktop app, *not* the `MoomooOpenD` CLI binary), min version **10.4.6408**, running and logged into a **paper** account. Install/upgrade via the skill:
   ```
   /install-moomoo-opend
   ```
   This also writes the version stamp at `~/.moomoo_skill_version`.

2. **Python ≥ 3.11** with the dependencies. From the repo root:
   ```bash
   python3 -m pip install moomoo-api tzdata "pydantic>=2.7"
   ```
   On macOS/Homebrew/Debian you may hit `error: externally-managed-environment` (PEP 668). Either use a virtualenv (recommended) or override:
   ```bash
   python3 -m pip install --break-system-packages moomoo-api tzdata "pydantic>=2.7"
   ```
   > Whichever interpreter you install into **must** be the same `python3` you run the trader and tests with — the vendored Moomoo scripts `import moomoo` from that environment.

3. **Confirm the install** before doing anything live:
   ```bash
   python3 -c "import moomoo, pydantic, zoneinfo; print('deps OK')"
   python3 -m pytest tests/ --ignore=tests/test_moomoo_broker_live.py -q   # expect: the count CI enforces — run `uv run pytest -q`
   ```

---

## 3. Configure

Risk limits and runtime knobs come from **environment variables** (nothing is hardcoded). The repo ships a non-secret template. **Config is not auto-loaded** — you `source` it so the values are exported into the trader's environment.

```bash
cp config/risk.config.example config/risk.config
# Review the limits — changing a RISK_* limit is a human-reviewed decision.
$EDITOR config/risk.config
```

Find your paper account id and put it in the environment:
```bash
python3 skills/moomooapi/scripts/trade/get_accounts.py   # note the SIMULATE acc_id
export FUTU_ACC_ID=<your SIMULATE acc_id>
```

### Configuration reference

**Risk limits — `config/risk.config` (`RISK_*`)** — changes require human review:

| Variable | Default (code) | Meaning |
|---|---|---|
| `RISK_TRADING_ENV` | `PAPER` | `PAPER` only in v1. `LIVE` is refused by `main()`. |
| `RISK_ALLOWED_SYMBOLS` | *(empty → nothing tradable)* | Comma list, e.g. `US.AAPL,US.MSFT,US.NIO`. The strategy's symbol is `STRATEGY_SYMBOL` if set (and in this list), else the lexicographically smallest symbol in the list. |
| `RISK_MIN_CONFIDENCE` | `0.6` | Signals below this confidence are dropped. |
| `RISK_MAX_ORDER_NOTIONAL` | `2000` | Per-order notional cap. |
| `RISK_MAX_POSITION_QTY` | `100` | Resulting-position share cap (long-only). |
| `RISK_DAILY_LOSS_LIMIT` | `500` | Halt trading when `day_pnl <= -limit`. |
| `RISK_MAX_GROSS_EXPOSURE` | `50000` | Gross exposure cap after an order. |
| `RISK_TRAILING_STOP_PCT` | `0.0` | Broker-resting trailing-stop % on each BUY entry. `0` disables; `5.0` = 5% (see §7). |
| `RISK_ALLOWED_OVERLAYS` | *(empty → options off)* | **DEFAULT OFF.** Comma-separated list of enabled overlays (case-insensitive): `COVERED_CALL`, `PROTECTIVE_PUT`, `COLLAR`, `BEAR_PUT_SPREAD`, `CALL_DIAGONAL`, `LEAP`. Requires human review before enabling. See `config/risk.config.example`. |
| `RISK_MAX_OPTION_CONTRACTS` | `0` | Per-leg contract cap. `0` = options fully disabled. |
| `RISK_OPTION_DEFAULT_CONTRACTS` | `1` | Contract count for strategies not sized off held shares — `BEAR_PUT_SPREAD`, `CALL_DIAGONAL`, `LEAP`. Covered call and collar still size off held shares (`min(shares//100, RISK_MAX_OPTION_CONTRACTS)`). |
| `RISK_OPTION_MAX_RISK_PCT` | `0.02` | **NLV-derived premium cap** (fraction of `total_assets`). Debit legs (long call/put, protective put, LEAP, long diagonal leg) cap premium paid at `NLV × pct`; credit legs (covered call, collar short call, spread/PMCC short leg) cap premium collected at `NLV × pct / 2` (the 200% stop ⟹ max loss = 2× premium). Scales automatically with equity — set `0.01` for a tighter 1% ceiling. |
| `RISK_MAX_OPTION_PREMIUM_PER_TRADE` | `0` (off) | Optional absolute dollar ceiling layered on top of the NLV cap; when set, the tighter of the two binds. `0` disables. |
| `RISK_OPTION_TARGET_DELTA` | `0.30` | Fallback delta target for legs that declare no per-leg override (covered call, protective put). Multi-leg strategies declare their own per-leg delta/DTE targets structurally in `options/overlays.py`. |
| `RISK_OPTION_DTE_MIN` / `RISK_OPTION_DTE_MAX` | `30` / `45` | Fallback DTE window for contract selection (same fallback scope as `RISK_OPTION_TARGET_DELTA`). |
| `RISK_OPTION_DTE_TO_CLOSE` | `7` | DTE at which an exit is declared (enforced in O4). |
| `RISK_OPTION_PROFIT_TARGET_PCT` | `0.5` | Profit-target fraction for exit (enforced in O4). |
| `RISK_UNREALIZED_LOSS_GATE` | `0.0` (off) | Unrealized-drawdown **entry block** in dollars. On breach, only NEW entries are blocked — open positions still exit via their trailing stops; this never auto-flattens (the hard HALT below stays realized-only). |
| `RISK_DAILY_LOSS_HALT` | *(unset; must exceed `RISK_DAILY_LOSS_LIMIT`)* | Hard daily-loss **flatten + halt** threshold. `RISK_DAILY_LOSS_LIMIT` only closes the entry gate (GATE); breaching this one flattens the book, cancels all orders, and halts trading for the day (HALT) when `day_pnl <= -value`. Persists to `engine_state` so a restart on a halted day stays halted. |
| `RISK_PER_TRADE_PCT` | `0.0` (off → fixed `ORDER_QTY`) | Risk-per-trade position sizing: fraction of `total_assets` risked to the stop distance. `qty = floor(equity * pct / stop_dist)`, scaled by confidence between `RISK_CONFIDENCE_SIZE_FLOOR`/`_CEIL`, then clamped by the notional/position/exposure caps above. |
| `RISK_CONFIDENCE_SIZE_FLOOR` / `RISK_CONFIDENCE_SIZE_CEIL` | `0.5` / `1.0` | Sizing scale bounds for `RISK_PER_TRADE_PCT` — floor applies at `RISK_MIN_CONFIDENCE`, ceiling at confidence `1.0`. |
| `RISK_REBALANCE_ENABLED` | `false` | Enables the midday `REBALANCE` job (§5) and its dry-run/live drift-band trading. **Off by default; Stage-3 flag in the live cutover — see `CUTOVER.md`.** |
| `RISK_REBALANCE_BAND_PCT` | `5.0` | Drift band (± percentage points of target weight) before a rebalance trade fires. |
| `RISK_REBALANCE_MIN_NOTIONAL` | `200` | Skip rebalance trades smaller than this notional. |
| `RISK_REBALANCE_CASH_BUFFER_PCT` | `10.0` | Fraction of the book reserved as cash; target weights apply to the remainder. |
| `RISK_TARGET_STALENESS_HOURS` | `24` | Skip the rebalance job if the ingested target-weight snapshot is older than this. |
| `RISK_MARKET_HOLIDAYS` | shipped default covers 2026 **and** 2027 (`autotrader/config.py:_DEFAULT_NYSE_HOLIDAYS`) | Comma-separated ISO dates added to the weekend gate in `market_calendar.is_trading_day`. `main()` logs a warning (`holiday_horizon_warning()`) once the calendar is within 60 days of running out — extend it well before then. Early-close (half-day) sessions are **not yet modeled** (see `PRE-LIVE.md`'s deferred list). |
| `RISK_LIMIT_ORDERS_ENABLED` | `false` | When enabled, entries/rebalance/option legs submit as capped marketable limits instead of plain MARKET orders (trailing-stop exits are unaffected). Rejected at config-load if both order caps below are `0`. **Off by default; Stage-3 flag — see `CUTOVER.md`.** |
| `RISK_ORDER_CAP_BPS` / `RISK_ORDER_CAP_TICKS` | `0` / `0` | Cap = `max(order_cap_bps/1e4 * price, order_cap_ticks * tick)` through the touch. At least one must be nonzero if `RISK_LIMIT_ORDERS_ENABLED=true`. |
| `RISK_ESCALATION_DWELL_SECONDS` | `20.0` | Seconds given to each limit-escalation stage (initial capped LIMIT, then the re-peg) before it's judged resting and escalation advances to the next stage. |
| `RISK_ACCOUNT_OWNERSHIP` | `SOLE` | `SOLE` = today's behavior — sweep/flatten/report the whole account. `SHARED` = every account-wide operation (stop reconcile, cancel-all, flatten, ground-truth sync) scopes to AutoTrader's own tracked book only, because this account is shared with an independent SNP trading bot (`com.bot.trading`, separate repo). **`RISK_TRADING_ENV=LIVE` + `RISK_ACCOUNT_OWNERSHIP=SHARED` is structurally refused** at config-load time — live money never runs with the scoped-down posture. |
| `RISK_SIGNALS_ENABLED` | `true` | Gates whether the webhook/file-drop signal inbox is consumed at all — `false` skips constructing the `SignalInbox` entirely, leaving the internal strategy + stops + halts running. This is the **Stage-1 cutover flag** in the live rollout (`CUTOVER.md`): Stage 1 sets it `0` so the first live session runs on the internal strategy alone. |

> **Partial fill (`OVERLAY_RESIDUAL_LONG`):** multi-leg overlays submit the long leg first. If a later (short) leg fails after the long has filled, the engine returns `OVERLAY_RESIDUAL_LONG` and leaves the long-only position in place — it is never a naked short. There is no automatic unwind; the residual is risk-defined by the long premium.
>
> **Deferred:** the 200% stop-loss for credit legs (converting the entry-discipline cap into a hard exit) is enforced in O4. The 5% buying-power rule is a further follow-up. Until then the no-naked-short coverage guard is the hard safety guarantee.

**OpenD connection (`FUTU_*`)** — read by the vendored `common.py`:

| Variable | Default | Meaning |
|---|---|---|
| `FUTU_OPEND_HOST` | `127.0.0.1` | OpenD host. |
| `FUTU_OPEND_PORT` | `11111` | OpenD port. |
| `FUTU_ACC_ID` | `0` | Your SIMULATE account id (set this explicitly). |

**Trader runtime (`AUTOTRADER_*` and friends)**:

| Variable | Default | Meaning |
|---|---|---|
| `ENTRY_BREAKOUT_LOOKBACK` | `20` | N completed daily bars for the breakout high. Higher = rarer, stronger breakouts. |
| `STRATEGY_ENABLED` | `true` | Internal strategy on/off. `false` = the engine acts only on webhook signals + rebalance. |
| `STRATEGY_SYMBOL` | *(unset → lexicographically smallest of `RISK_ALLOWED_SYMBOLS`)* | Pins the internal strategy to one symbol (must also be in `RISK_ALLOWED_SYMBOLS`). |
| `STRATEGY_STOP_LOSS_PCT` | `0.05` | Internal breakout strategy's stop-loss, as a fraction below entry. |
| `STRATEGY_TAKE_PROFIT_PCT` | `0.10` | Internal breakout strategy's take-profit, as a fraction above entry. |
| `STRATEGY_CONFIDENCE` | `0.7` | Fixed confidence the internal strategy attaches to its own signals (must clear `RISK_MIN_CONFIDENCE`). |
| `ORDER_QTY` | `1` | Shares per BUY entry (used when `RISK_PER_TRADE_PCT` sizing is off). |
| `AUTOTRADER_LOOP_INTERVAL` | `5` | Seconds between loop iterations. |
| `AUTOTRADER_DB_PATH` | `~/.autotrader.db` | SQLite projection (positions/fills/perf). |
| `AUTOTRADER_AUDIT_PATH` | `~/.autotrader_trade_audit.jsonl` | Trade-audit journal path (see §10). Deliberately different from the vendored skills'/SNP bot's `~/.futu_trade_audit.jsonl` so the two audit trails never collide on the shared paper account. |
| `AUTOTRADER_SIGNAL_INBOX` | *(unset → ingress off)* | Directory watched for external signal files (see §6). |
| `AUTOTRADER_SIGNAL_TTL_HOURS` | `24` | Signals (file-drop or webhook-enqueued) older than this are quarantined, not routed. |
| `AUTOTRADER_SNAPSHOT_CACHE_TICKS` | `6` | Engine ticks an account snapshot is cached for before refreshing (feeds strategy evaluation only; order routing always re-fetches). `get_account()` costs 2 of Moomoo's 10-refresh/30s-per-account budget — keep this **≥ 6** (≈30s at the default 5s loop interval), and higher still in `RISK_ACCOUNT_OWNERSHIP=SHARED` since that budget is shared with the SNP bot's own queries against the same OpenD instance. |
| `AUTOTRADER_LOG_DIR` | *(unset → stdout only)* | Directory for the optional rotating on-disk log, independent of launchd's own stdout/stderr redirection (§13). |
| `AUTOTRADER_HEARTBEAT_URL` | *(unset → off)* | Dead-man's-switch heartbeat URL, pinged on a fixed cadence so an externally-monitored, silently-wedged process is detectable (§13). |
| `AUTOTRADER_HEARTBEAT_EVERY` | `60` | Loop iterations between heartbeat pings when `AUTOTRADER_HEARTBEAT_URL` is set. |
| `AUTOTRADER_SLACK_WEBHOOK_URL` | *(unset → EOD report off)* | Slack incoming-webhook URL for the 16:30 ET EOD summary and operational alerts (job failures, HALT, watchdog-unhealthy, repeated loop errors). Lives in `config/secure.config`. |
| `AUTOTRADER_WEBHOOK_SECRET` | *(required for webhook ingress)* | HMAC-SHA256 signing secret for the webhook (§12b). Lives in `config/secure.config`. |
| `AUTOTRADER_WEBHOOK_TOKEN` | *(unset → falls back to `AUTOTRADER_WEBHOOK_SECRET` for both header checks, with a startup warning)* | Independent bearer token for `X-Webhook-Token`, separate from the HMAC signing secret — a leak of one header alone no longer grants forgery. Set both for two-secret mode (§12b). |
| `AUTOTRADER_WEBHOOK_HOST` / `AUTOTRADER_WEBHOOK_PORT` | `127.0.0.1` / `8799` | Bind address for the webhook receiver process (§12b). Keep the host `127.0.0.1` — only ngrok (or an equivalent tunnel) should make it public. |
| `AUTOTRADER_WEBHOOK_MAX_BODY` | `65536` (64 KiB) | Max accepted request body size; larger requests get **413** (see §12). |
| `AUTOTRADER_WEBHOOK_FRESHNESS_MIN` | `15` | Max age (minutes, both past and future) accepted for a webhook payload's `timestamp` before rejection. |
| `OPEND_READY_TIMEOUT` | `30` | Seconds to wait for OpenD readiness at startup before halting. |

> ℹ️ **Breakout entry.** The internal strategy enters only on a new N-day high (`price > the highest high of the last ENTRY_BREAKOUT_LOOKBACK completed daily bars`). If the daily klines can't be fetched, it does **not** enter — it never buys at open. Entries still fire only inside the 09:45–15:30 ET window (§5).

---

## 4. Run the trader (continuous loop)

```bash
cd /path/to/AutoTrader
set -a && source config/risk.config && set +a       # export all RISK_* vars
set -a && source config/secure.config && set +a     # AUTOTRADER_SLACK_WEBHOOK_URL (EOD report) + secrets
export FUTU_ACC_ID=<your SIMULATE acc_id>
export ENTRY_BREAKOUT_LOOKBACK=20   # N-day breakout window (default)

python3 -m autotrader.main
```

> **EOD Slack report:** the end-of-day summary (§5, 16:30 ET) only posts if `AUTOTRADER_SLACK_WEBHOOK_URL` is in the trader's environment — it lives in `config/secure.config`, so you **must** source that file too (above). At startup the trader logs either `EOD Slack reporter enabled` or `EOD Slack reporter disabled (AUTOTRADER_SLACK_WEBHOOK_URL unset)` — check which you got.

**Startup sequence** (logged to stdout): logs `TRADING_ENV=PAPER` → exponential-backoff **readiness gate** waits for OpenD (halts after `OPEND_READY_TIMEOUT`) → connects the broker → opens the SQLite projection → starts `SessionRunner.run()`. The process then runs continuously until you stop it (§8).

It is safe to start the trader **before** market open — the lifecycle scheduler catches up missed jobs on the first poll.

---

## 5. Daily lifecycle (what the loop does, in US/Eastern)

Each iteration: run any newly-due lifecycle jobs → `watchdog.ensure_healthy()` (if the connection is down it backs off and reconciles before resuming; it **never trades while unhealthy**) → one engine tick → poll the external-signal inbox (if configured). The **eight** daily jobs (America/New_York, DST-aware), in chronological order:

| Time (ET) | Job | Effect |
|---|---|---|
| **08:30** | `PRE_OPEN_SYNC` | Pull broker ground truth (positions + fills) into the SQLite projection. |
| **09:45** | `ENTRY_OPEN` | Open the entry window — **new BUY entries are now allowed**. Reconciles trailing stops first (§7), then replays any deferred pre-market signals. |
| **12:30** | `REBALANCE` | Drift-band rebalance toward the latest target weights (`RISK_REBALANCE_ENABLED`) + trailing-stop consolidation. No-op if disabled, no target snapshot, or the snapshot is stale (`RISK_TARGET_STALENESS_HOURS`). |
| **13:30** | `RISK_CHECK_MID` | Tiered intraday risk re-check (GATE/HALT) + ground-truth sync + performance snapshot + trailing-stop reconcile (skipped if halted). |
| **15:00** | `RISK_CHECK_LATE` | Same as `RISK_CHECK_MID`. |
| **15:30** | `RISK_SWEEP` | Close the entry window, re-sync ground truth, record performance, reconcile stops (skipped if halted). |
| **16:15** | `EOD_CANCEL_ORDERS` | Cancel all working orders (`cancel_working_orders()` — whole-account in `SOLE`, tracked-only in `SHARED`), sync fills, commit the day's final performance row. **Positions are NOT flattened** — this only cancels orders (the internal name `EOD_FLATTEN` is a deprecated alias kept for readable old logs/persisted state; nothing is flattened here). Stops re-attach at the next `ENTRY_OPEN`. |
| **16:30** | `EOD_REPORT` | Post the end-of-day summary to Slack (§4). |

**Retry semantics (mark-on-success):** `LifecycleScheduler.poll(now)` is read-only — it only *reports* which jobs are due; it does not mark them done. The runner calls `mark_fired(name, now)` itself, and only after that job's handler returns without raising. A job that raises is logged and alerted (`AlertSink`, key `job-fail:{job}:{date}`) but is **not** marked fired, so it retries on the very next poll — and, critically, one job's failure never blocks the rest of that poll's due batch (each job runs in its own try/except). `REBALANCE`, `EOD_CANCEL_ORDERS`, and `EOD_REPORT` are additionally **at-most-once per day** even across a process restart (durable state in `engine_state`, keyed `sched:<job>`) — a restart after 16:30 will not re-run the rebalance or re-post the EOD report. The other five jobs deliberately re-fire on catch-up (e.g. a process that starts at 10:00 still runs `PRE_OPEN_SYNC` and `ENTRY_OPEN` immediately) — that is the intended halt/restart-recovery behavior, not a bug.

**Entry gating:** BUY entries only place between **09:45 and 15:30**. Outside the window a BUY reports `ENTRY_CLOSED`. **SELL exits are never gated** — you can always flatten a position.

---

## 6. External signals (optional file-drop ingress)

The trader can ingest external signals via a **local directory** (no network port — localhost-only by construction). Enable it:

```bash
export AUTOTRADER_SIGNAL_INBOX=~/.autotrader_inbox
python3 -m autotrader.main
```

Drop a JSON file matching `RoutineSignalPayload` into that directory. Example `~/.autotrader_inbox/sig1.json`:

```json
{
  "routine_id": "r1",
  "timestamp": "2026-06-13T09:46:00-04:00",
  "signal_changes": [
    { "ticker": "AAPL", "direction": "UP", "transition": ["50", "200"], "points_delta": 10, "driver": "breakout" }
  ],
  "hard_stops": {},
  "catalysts": []
}
```

Normalization rules:
- `direction`: `UP` → BUY, `DOWN` → SELL.
- `ticker`: bare `AAPL` → `US.AAPL` (already-qualified symbols pass through, upper-cased). **Must be in `RISK_ALLOWED_SYMBOLS`** or the risk core rejects it.
- `confidence`: `|points_delta| / 10`, clamped to `[0,1]` (so `points_delta` ≥ 10 → 1.0; `0` → dropped). Must clear `RISK_MIN_CONFIDENCE`.

After each poll the file is moved to `inbox/processed/` (valid) or `inbox/rejected/` (malformed — a bad file never crashes the loop). External signals are only polled while the watchdog is healthy and route through the **same** risk core + audited router as strategy signals.

### 6a. Ticker-sweep adapter

The `trade-routine` skill emits a daily **ticker sweep** (`docs/routinesignal-ticker.json`) in a *different* dialect (`run_id` / `sweep_date` / `from_signal`→`to_signal` / `composite_score` / `direction: "upgrade"|"downgrade"`). That shape is **rejected** by the ingress as-is — convert it first:

```bash
export AUTOTRADER_SIGNAL_INBOX=~/.autotrader_inbox
export RISK_ALLOWED_SYMBOLS=US.DIVO,CA.VDY,US.YNVDA      # qualified codes
python3 -m autotrader.signals.routine_adapter routinesignal-ticker.json   # or: cat … | python3 -m … -
```

It drops one canonical `RoutineSignalPayload` into the inbox (or pipe its stdout to the signed webhook curl below). Mapping:
- **Direction from the destination label** (not the up/down field): `BUY`/`STRONG BUY` → UP (entry); `CAUTION`/`AVOID` → DOWN (exit); `HOLD`/`NEUTRAL` and unknown labels → **skipped** (so an "upgrade to NEUTRAL" never buys).
- **Exits always clear the confidence filter:** SELL gets `points_delta = -10` (confidence 1.0) so a low-score `AVOID` is never dropped; BUY scales as `round(composite_score/10)`.
- **Symbols qualified against `RISK_ALLOWED_SYMBOLS`** (bare `VDY` → `CA.VDY`); unknown/ambiguous tickers are skipped with a warning.

Exit codes: `0` ok (incl. 0 actionable → nothing enqueued), `1` bad input JSON, `2` `AUTOTRADER_SIGNAL_INBOX` unset. The adapter is SDK-free and never touches OpenD.

---

## 7. Trailing stops

Set `RISK_TRAILING_STOP_PCT=5.0` (in `config/risk.config`) to attach a broker-resting `TRAILING_STOP` SELL to every BUY entry — a protective exit that survives an OpenD outage. `0.0` disables it.

**Entry-fill-confirm before attaching.** A live MARKET BUY is acknowledged as `SUBMITTED` (async fill), not filled immediately. `_attach_trailing_stop` no longer trusts the immediate post-submit snapshot: it calls `_confirm_off_book`, which polls the broker's open-orders list (bounded attempts, exponential backoff) until the entry order is confirmed off the book (filled or otherwise terminal) before re-fetching the account snapshot and attaching the stop. Against the in-memory `SimBroker` (synchronous fills) this confirms on the first check; against live OpenD it may take a few poll attempts.

**Intraday reconcile backstop.** Even if an entry-time attach is skipped or rejected, `StopManager.reconcile` runs at four points in the daily lifecycle — `ENTRY_OPEN` (morning re-attach; every working order was cancelled the prior `EOD_CANCEL_ORDERS` and Moomoo orders are DAY time-in-force anyway), `RISK_CHECK_MID`, `RISK_CHECK_LATE`, and `RISK_SWEEP` (§5). The `RISK_CHECK_MID` / `RISK_CHECK_LATE` / `RISK_SWEEP` call sites are skipped while the entry gate is halted (a flattened book needs no stops); the `ENTRY_OPEN` call site has no halted-guard and always runs regardless of gate state. Each reconcile: (1) sweeps orphaned working orders (stops for closed positions, unrecognized orders — in `SHARED` mode, scoped to AutoTrader's own tracked book only, §3), then (2) attaches a stop for every held long that doesn't already have one working. A position that still can't get a stop (no quote available, or the attach itself is rejected) logs an `⚠ UNPROTECTED` error and fires a Slack alert (`AlertSink`, if configured) rather than failing silently — check the trader log or Slack for that line if you suspect a gap, and the "stop reconcile: attached=%d failed=%d orphans=%d protected=%d skipped=%d" summary line after every reconcile.

---

## 8. Stopping the trader

Press **Ctrl-C**. The shutdown path calls `cancel_working_orders()` — whole-account `cancel_all()` in `RISK_ACCOUNT_OWNERSHIP=SOLE` (the default), or only AutoTrader's own tracked orders (`cancel_tracked_orders`) in `SHARED` — and disconnects cleanly; a failed cancel is logged but never leaks the connection. Always stop with Ctrl-C rather than `kill -9` so cancel-on-shutdown runs.

Running under launchd supervision (§13)? Ctrl-C doesn't apply to a background service — use `launchctl unload ~/Library/LaunchAgents/com.autotrader.trader.plist` instead, which still runs the same shutdown path before the process exits (unlike killing the PID directly, launchd will not restart it after an `unload`).

---

## 9. Run the dashboard (read-only monitoring)

In a **separate terminal** (it also needs OpenD running):

```bash
# pip:
pip install -r dashboard/requirements.txt && python3 dashboard/server.py
# or uv (isolated env):
uv venv && uv pip install -r dashboard/requirements.txt && uv run python dashboard/server.py
```

Serves **http://127.0.0.1:8787/**. Endpoints: `/api/health`, `/api/accounts`, `/api/snapshot` (auto-detects the first SIMULATE account). It is structurally read-only (a hardcoded allow-list of four read scripts) and gates every endpoint on the OpenD health check — endpoints return **503** until OpenD is ready.

Dashboard presentation settings come from `config/dashboard.config` (copy from `config/dashboard.config.example`) or `DASHBOARD_*` env vars. **Keep `refresh_seconds >= 20`** — each refresh makes ~4 cache-refreshing calls and Moomoo caps those at 10 per 30s per account.

---

## 10. Observability — where to look

| What | Where |
|---|---|
| **Audit journal** (immutable order record: `intent` then `ack` per order, written before placement) | `~/.autotrader_trade_audit.jsonl` (or `$AUTOTRADER_AUDIT_PATH`) — deliberately distinct from the vendored skills'/SNP bot's `~/.futu_trade_audit.jsonl` |
| **SQLite projection** (tables: `signals`, `trades`, `fills`, `positions`, `performance`, `halts`, `target_weights`, `drivers`, `engine_state`) | `~/.autotrader.db` (or `$AUTOTRADER_DB_PATH`) |
| **Live account view** | Dashboard at `:8787`, or `python3 skills/moomooapi/scripts/trade/get_portfolio.py` |
| **Trader logs** | stdout of the `python3 -m autotrader.main` process, or the rotating file under `$AUTOTRADER_LOG_DIR` / `~/Library/Logs/autotrader/` if supervised (§13) |

Quick peeks:
```bash
tail -f "${AUTOTRADER_AUDIT_PATH:-$HOME/.autotrader_trade_audit.jsonl}"
sqlite3 ~/.autotrader.db "SELECT * FROM performance ORDER BY date DESC LIMIT 5;"
sqlite3 ~/.autotrader.db "SELECT side,qty,order_type,state FROM trades ORDER BY id DESC LIMIT 10;"
```

---

## 11. Verify before you run

```bash
# Offline core (no OpenD needed): expect the count CI enforces (.github/workflows/tests.yml) — run it and read the summary line, don't hardcode a number here.
python3 -m pytest tests/ --ignore=tests/test_moomoo_broker_live.py -q
uv run pytest -q     # equivalent, if you use uv

# Live adapter smoke test (OpenD running, paper account):
RUN_LIVE=1 python3 -m pytest tests/test_moomoo_broker_live.py
```

---

## 12. Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `halting: OpenD not ready` at startup | OpenD GUI not running/authenticated, or wrong host/port. Start & log into OpenD; check `FUTU_OPEND_HOST/PORT`. Raise `OPEND_READY_TIMEOUT` if it's just slow. |
| Dashboard returns **503** | Same — OpenD not ready yet. It will serve once readiness passes. |
| `ModuleNotFoundError: pydantic` (or `moomoo`) | Installed into a different interpreter than the one running the trader. Reinstall into the same `python3` (see §2; PEP 668 → `--break-system-packages` or a venv). |
| Strategy never enters | No new N-day high yet, or the daily klines could not be fetched (fail-safe = no entry). Check logs for "breakout: no N-day high". |
| BUY reports `ENTRY_CLOSED` | Outside the 09:45–15:30 ET entry window — by design. SELL exits still work. |
| Order `REJECTED_BY_RISK` | A risk limit blocked it (notional / position / exposure / daily-loss / not in `RISK_ALLOWED_SYMBOLS` / stale snapshot). The reason is logged. |
| External signal ignored | File moved to `inbox/rejected/` (malformed), confidence below `RISK_MIN_CONFIDENCE`, symbol not in `RISK_ALLOWED_SYMBOLS`, or the watchdog was unhealthy that tick. |
| Trailing stop didn't appear after a live entry | Attach confirms the fill first (bounded poll) — see §7; if the poll timed out, the next `RISK_CHECK_MID`/`RISK_CHECK_LATE`/`RISK_SWEEP`/`ENTRY_OPEN` reconcile is the backstop. Check for an `⚠ UNPROTECTED` log line / Slack alert. |
| Webhook returns **401** | Missing/wrong `X-Webhook-Token`, or the `X-Webhook-Signature` HMAC doesn't match the raw body. Recompute the signature over the EXACT bytes sent (see §12b). |
| Webhook returns **413** | Body exceeds `AUTOTRADER_WEBHOOK_MAX_BODY`. Send a smaller payload or raise the cap. |
| Webhook **202** but no trade | Working as designed — the webhook only enqueues. The trade happens on the trader's next healthy poll, inside 09:45–15:30 ET, if it clears the risk core. Check the trader log / `inbox/processed/` and `inbox/rejected/`. |
| `unlock_trade` requested | Never done programmatically. Unlock manually in the OpenD GUI. |

---

## 12b. Webhook ingress (ngrok) — Phase 2c-W

Deliver signals over HTTP instead of dropping files. The webhook is a **separate, internet-facing process with no broker access**: it authenticates, validates the payload, and atomically writes it into the same `AUTOTRADER_SIGNAL_INBOX` the trader already polls (§6). OpenD is never exposed; every trade still clears the risk core and the 09:45–15:30 window.

**Topology — three processes on one machine:**

```bash
# Terminal A — trader (broker-connected, NOT internet-exposed):
set -a && source config/risk.config && set +a
export FUTU_ACC_ID=<your SIMULATE acc_id> AUTOTRADER_SIGNAL_INBOX=~/.autotrader_inbox
python3 -m autotrader.main

# Terminal B — webhook receiver (localhost only, NO OpenD access):
set -a && source config/secure.config && set +a          # AUTOTRADER_WEBHOOK_SECRET, host/port
export AUTOTRADER_SIGNAL_INBOX=~/.autotrader_inbox        # SAME dir as the trader
python3 -m autotrader.signals.webhook                     # binds 127.0.0.1:8799

# Terminal C — expose Terminal B publicly:
ngrok http 8799    # forwards https://<your-tunnel>.ngrok-free.dev -> 127.0.0.1:8799
```

**Send a signed request.** Both headers are required; the signature is HMAC-SHA256 of the *exact* request body, keyed by the secret:

```bash
SECRET="$AUTOTRADER_WEBHOOK_SECRET"
BODY='{"routine_id":"r1","timestamp":"2026-06-13T09:46:00-04:00","signal_changes":[{"ticker":"AAPL","direction":"UP","transition":["50","200"],"points_delta":10,"driver":"breakout"}]}'
SIG="sha256=$(printf '%s' "$BODY" | openssl dgst -sha256 -hmac "$SECRET" | awk '{print $2}')"
curl -sS -X POST https://<your-tunnel>.ngrok-free.dev/webhook/sweep \
  -H "X-Webhook-Token: $SECRET" -H "X-Webhook-Signature: $SIG" \
  -H "Content-Type: application/json" --data "$BODY"
# -> {"status":"accepted","routine_id":"r1","signals":1}
```

`GET /healthz` is unauthenticated and returns `{"status":"ok"}` for liveness checks.

**Payload** = the same `RoutineSignalPayload` as the file-drop (§6): `ticker` UP→BUY / DOWN→SELL, normalized to `US.<TICKER>`, confidence from `|points_delta|/10`. The symbol must be in `RISK_ALLOWED_SYMBOLS`.

**Rejected payloads (400) are preserved, not lost.** A request that authenticates but fails schema validation returns `400` with the offending fields named, e.g.:

```json
{"error":"invalid payload","detail":[{"loc":"hard_stops.ticker","msg":"Input should be a valid number..."},
                                      {"loc":"catalysts.0.value","msg":"Field required"}]}
```

The exact rejected body is atomically quarantined to `<inbox>/rejected/` (mirroring the file-drop's `rejected/`), so you can inspect the `detail`, fix the producer, and replay it. The `detail` is only returned **after** auth passes, so it never leaks the schema to unauthenticated callers. Validation is never relaxed — a bad payload is captured and explained, never enqueued. (`hard_stops` must be a flat `{symbol: price}` map and every `catalyst` needs a numeric `value` — the two most common producer mistakes.)

**Security notes:**
- Secrets live in `config/secure.config` only (gitignored — see `config/secure.config.example`). Generate them with `python3 -c "import secrets;print(secrets.token_urlsafe(32))"` and rotate periodically. `AUTOTRADER_WEBHOOK_SECRET` (HMAC signing) and `AUTOTRADER_WEBHOOK_TOKEN` (bearer header) are **independent** — set both so a leak of one header alone doesn't grant forgery; if `AUTOTRADER_WEBHOOK_TOKEN` is unset, the webhook falls back to single-secret mode (both checks use `AUTOTRADER_WEBHOOK_SECRET`) and logs a startup warning.
- The webhook binds `127.0.0.1` (`AUTOTRADER_WEBHOOK_HOST`); only ngrok makes it public. Harden the ngrok edge too (IP allow-list / Basic-Auth) as defense-in-depth.
- **Replay/freshness defense in depth:** the webhook rejects a payload whose `timestamp` is outside `AUTOTRADER_WEBHOOK_FRESHNESS_MIN` (default 15 min, both past and future) with a 401-adjacent rejection before it ever reaches the inbox; the consumer additionally dedupes by `routine_id` (`SignalInbox`, via `db.get_state`/`set_state`) and quarantines anything older than `AUTOTRADER_SIGNAL_TTL_HOURS` (default 24h) — this second layer also covers the file-drop adapter path, which bypasses the webhook entirely. A signal that arrives while the trader is down simply queues in the inbox and fires on the next healthy poll.
- 401 audit writes (failed auth attempts) are truncated, size-capped, and throttled (10 per 60s per remote address) so a hostile client can't grow the audit file unbounded.
- The webhook **cannot place an order** — it has no broker handle. The risk core, paper-only enforcement, and all caps remain the backstop even for an authenticated-but-bad payload.

---

## 13. Supervision (launchd), logging, heartbeat, backups

For anything beyond an interactive terminal session, run the trader (and
optionally the webhook) as macOS `launchd` agents that restart on crash and
on reboot, with rotated file logs, a dead-man heartbeat, and nightly SQLite
backups. This is documented in full in **`deploy/README.md`** — install
steps, verification, uninstall, the dead-man heartbeat, recommended power
settings (`pmset`), and a manual verification checklist (kill → restart,
reboot → both agents return, heartbeat pings, backup file + restore drill).
This section is deliberately a pointer, not a duplicate — go there for the
actual commands.

Quick orientation:

| Artifact | Purpose |
|---|---|
| `deploy/com.autotrader.trader.plist` | The continuous trading loop, `KeepAlive` + `RunAtLoad`. |
| `deploy/com.autotrader.webhook.plist` | The webhook signal ingress (§12b), same restart behavior. Only needed if you use the webhook instead of / alongside the file-drop inbox. |
| `deploy/com.autotrader.backup.plist` | Nightly backup, calendar-triggered at 17:30 local. |
| `deploy/backup_autotrader.sh` | `VACUUM INTO`s the SQLite projection, copies the trade-audit journal, prunes anything older than 30 days. |

Labels are `com.autotrader.*` — a separate, independent process (the SNP
trading bot, its own repo) already owns `com.bot.trading` on the shared Mac;
do not reuse or collide with that label, and do not point any of these units
at that bot's files. `FUTU_ACC_ID` must be set in `config/secure.config`
(not exported by hand) for a launchd-supervised process, since it has no
interactive shell — see `deploy/README.md` §1.

---

## 14. Live trading

**Intentionally blocked in v1.** `main()` exits if `RISK_TRADING_ENV != PAPER`. Going live is a separate, human-gated phase requiring a two-key unlock (`TRADING_ENV=LIVE` **and** a manual GUI unlock), reduced caps, and a tested kill-switch. Do not attempt to force it here. When that phase begins, follow `CUTOVER.md` — it is the authoritative, staged procedure (preconditions, the two-file guard, the exact env diff, the kill-switch test, rollback, and stage-promotion criteria) and `PRE-LIVE.md` is the gate it depends on.

---

*Architecture & rationale:* `docs/research/autotrader-architecture-research.md` · `docs/superpowers/plans/`. *Project rules:* `CLAUDE.md`.
