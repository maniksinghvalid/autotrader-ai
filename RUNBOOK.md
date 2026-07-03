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
   python3 -m pytest tests/ --ignore=tests/test_moomoo_broker_live.py -q   # expect: 118 passed
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
| `ORDER_QTY` | `1` | Shares per BUY entry. |
| `AUTOTRADER_LOOP_INTERVAL` | `5` | Seconds between loop iterations. |
| `AUTOTRADER_DB_PATH` | `~/.autotrader.db` | SQLite projection (positions/fills/perf). |
| `AUTOTRADER_SIGNAL_INBOX` | *(unset → ingress off)* | Directory watched for external signal files (see §6). |
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

Each iteration: run any newly-due lifecycle jobs → `watchdog.ensure_healthy()` (if the connection is down it backs off and reconciles before resuming; it **never trades while unhealthy**) → one engine tick → poll the external-signal inbox (if configured). The four daily jobs (America/New_York, DST-aware):

| Time (ET) | Job | Effect |
|---|---|---|
| **08:30** | `PRE_OPEN_SYNC` | Pull broker ground truth (positions + fills) into the SQLite projection. |
| **09:45** | `ENTRY_OPEN` | Open the entry window — **new BUY entries are now allowed**. |
| **15:30** | `RISK_SWEEP` | Close the entry window, re-sync ground truth, record a performance snapshot. |
| **16:15** | `EOD_FLATTEN` | `cancel_all()` working orders, close entries, commit performance. |

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

> **Live caveat (fail-safe):** against the in-memory sim broker a BUY fills synchronously and the stop attaches immediately. On live OpenD a MARKET BUY is acknowledged as `SUBMITTED` (async fill), so the just-entered position may not yet appear when the stop's long-only check runs; the stop may **skip attaching that tick** (the entry is left unprotected, but a short can never open — it is fail-safe, never mis-directed). Attaching off a reconciled fill is a Phase 3 follow-up. Until then, watch open orders in the dashboard after an entry.

---

## 8. Stopping the trader

Press **Ctrl-C**. The shutdown path cancels all working orders (`cancel_all`) and disconnects cleanly — a failed cancel is logged but never leaks the connection. Always stop with Ctrl-C rather than `kill -9` so cancel-on-shutdown runs.

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
| **Audit journal** (immutable order record: `intent` then `ack` per order, written before placement) | `~/.futu_trade_audit.jsonl` |
| **SQLite projection** (tables: `signals`, `trades`, `fills`, `positions`, `performance`, `halts`) | `~/.autotrader.db` (or `$AUTOTRADER_DB_PATH`) |
| **Live account view** | Dashboard at `:8787`, or `python3 skills/moomooapi/scripts/trade/get_portfolio.py` |
| **Trader logs** | stdout of the `python3 -m autotrader.main` process |

Quick peeks:
```bash
tail -f ~/.futu_trade_audit.jsonl
sqlite3 ~/.autotrader.db "SELECT * FROM performance ORDER BY date DESC LIMIT 5;"
sqlite3 ~/.autotrader.db "SELECT side,qty,order_type,state FROM trades ORDER BY id DESC LIMIT 10;"
```

---

## 11. Verify before you run

```bash
# Offline core (no OpenD needed):
python3 -m pytest tests/ --ignore=tests/test_moomoo_broker_live.py -q     # expect 118 passed, 0 skipped

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
| Trailing stop didn't appear after a live entry | Expected fail-safe behavior on async fills — see §7. |
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
ngrok http 8799    # forwards https://unthawed-keshia-unplenteously.ngrok-free.dev -> 127.0.0.1:8799
```

**Send a signed request.** Both headers are required; the signature is HMAC-SHA256 of the *exact* request body, keyed by the secret:

```bash
SECRET="$AUTOTRADER_WEBHOOK_SECRET"
BODY='{"routine_id":"r1","timestamp":"2026-06-13T09:46:00-04:00","signal_changes":[{"ticker":"AAPL","direction":"UP","transition":["50","200"],"points_delta":10,"driver":"breakout"}]}'
SIG="sha256=$(printf '%s' "$BODY" | openssl dgst -sha256 -hmac "$SECRET" | awk '{print $2}')"
curl -sS -X POST https://unthawed-keshia-unplenteously.ngrok-free.dev/webhook/sweep \
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
- The secret lives in `config/secure.config` only (gitignored — see `config/secure.config.example`). Generate it with `python3 -c "import secrets;print(secrets.token_urlsafe(32))"` and rotate it periodically.
- The webhook binds `127.0.0.1`; only ngrok makes it public. Harden the ngrok edge too (IP allow-list / Basic-Auth) as defense-in-depth.
- A signal that arrives while the trader is down simply queues in the inbox and fires on the next healthy poll. There is no replay/staleness window yet (deferred) — keep the secret tight.
- The webhook **cannot place an order** — it has no broker handle. The risk core, paper-only enforcement, and all caps remain the backstop even for an authenticated-but-bad payload.

---

## 13. Live trading

**Intentionally blocked in v1.** `main()` exits if `RISK_TRADING_ENV != PAPER`. Going live is a separate, human-gated phase requiring a two-key unlock (`TRADING_ENV=LIVE` **and** a manual GUI unlock), reduced caps, and a tested kill-switch. Do not attempt to force it here.

---

*Architecture & rationale:* `docs/research/autotrader-architecture-research.md` · `docs/superpowers/plans/`. *Project rules:* `CLAUDE.md`.
