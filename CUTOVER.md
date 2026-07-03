# CUTOVER — Paper to Live

This is the procedure for flipping AutoTrader from paper (`RISK_TRADING_ENV=PAPER`)
to live (`RISK_TRADING_ENV=LIVE`) trading real money on the shared Moomoo account,
in three deliberately narrow stages. It assumes `PRE-LIVE.md` Stage 1 is fully
checked off — **do not start this document until that gate is clean.**

Everything here is additive to, not a replacement for, `RUNBOOK.md` (day-to-day
operation) and `PRE-LIVE.md` (the readiness gate). If any step below conflicts
with those two, stop and reconcile before proceeding — this document is the
*sequence of actions*, they are the *acceptance criteria*.

---

## 1. Preconditions

Before touching any config:

- [ ] Every PRE-LIVE.md **Stage 1** box is checked (V1–V11 code gates, plus the
      Stage-1 operational boxes: supervision reboot test, backup restore drill,
      alert live-fire, holiday horizon, RUNBOOK refreshed).
- [ ] A **supervised paper session** has been run in `SOLE` mode with the
      independent SNP trading bot (`com.bot.trading`) **stopped**. This closes
      the SHARED/SOLE rehearsal gap: every V1–V10 regression test and every
      paper session to date ran against a live SNP bot sharing the account
      (`RISK_ACCOUNT_OWNERSHIP` defaults to `SOLE`, and `SOLE` behavior is
      untouched by V11 — see `ground_truth_sync(owned_only=False)` in
      `autotrader/lifecycle.py`), but a `SOLE`-mode cutover must be rehearsed
      with the SNP bot *actually absent*, not merely "assumed harmless." Stop
      `com.bot.trading` (`launchctl unload` its own plist — that is a
      different repo's artifact, not `deploy/`), run a full paper session
      end-to-end, and confirm no behavior depended on the other bot's
      presence.
- [ ] Backups are verified **restorable**, not just "the file exists": run
      `deploy/backup_autotrader.sh` (or wait for its 17:30 launchd firing),
      then `sqlite3 ~/.autotrader_backups/autotrader-<date>.db ".tables"` and
      confirm it opens and the expected tables (`signals`, `trades`, `fills`,
      `positions`, `performance`, `halts`, `engine_state`) are populated.
- [ ] Alerts are verified **firing**, not just wired: force a test HALT on
      paper (e.g. temporarily set `RISK_DAILY_LOSS_HALT` to a value the
      current paper day's `day_pnl` already breaches, restart, let
      `apply_risk_check` trip `RiskAction.HALT`) and confirm the Slack
      message actually lands (`AlertSink.send` keyed `halt:<date>` —
      `autotrader/alerts.py`). Revert the config value immediately after
      (risk-limit changes need human review — do not leave a probe value in
      `config/risk.config`).

Do not proceed past this section until all four boxes are checked by a human,
not inferred from "the code looks right."

---

## 2. The two-file guard revision

There are exactly **two** files where the paper-only guard lives, and cutover
touches nothing else in the safety spine:

| File | Guard | What changes |
|---|---|---|
| `autotrader/main.py` | `main()` logs `TRADING_ENV=%s` then `if cfg.trading_env != "PAPER": ... refuse to start` | The **value** of `RISK_TRADING_ENV` changes (via config, not code) to `LIVE`. The guard code itself is not edited. |
| `autotrader/risk_core.py` | `evaluate()`'s first universal check: `if cfg.trading_env != "PAPER": return RiskDecision(False, ...)` | Same — this is config-driven (`cfg.trading_env`), not a code edit either. |

**This means the "two-file guard revision" for v1 cutover is a config change,
not a code change** — `RISK_TRADING_ENV=PAPER` is read from the environment in
both places, so setting it to `LIVE` in `config/risk.config` is sufficient to
pass both gates. There is nothing to patch in `main.py` or `risk_core.py`
themselves for this plan's scope.

If a future revision needs to *literally* edit either guard (e.g. adding a
third environment value, or changing the refusal condition), that edit:

- touches **only** these two files (no other file should ever gate on
  `trading_env` directly — grep `trading_env` across `autotrader/` before
  merging such a change to confirm),
- gets **human code review** before merge, no exceptions,
- is verified against `RISK_ACCOUNT_OWNERSHIP=SOLE` still being asserted:
  `autotrader/config.py`'s `load_risk_config()` raises `ValueError` at
  config-load time if `RISK_TRADING_ENV=LIVE` and `RISK_ACCOUNT_OWNERSHIP=SHARED`
  are set together (`tests/test_account_ownership_config.py`) — live money
  never runs with the scoped-down SHARED safety posture. Confirm this check
  is still present and still raises (a quick `RISK_TRADING_ENV=LIVE
  RISK_ACCOUNT_OWNERSHIP=SHARED python3 -c "from autotrader.config import
  load_risk_config; load_risk_config()"` should raise `ValueError`) before
  and after any such edit.

---

## 3. Stage 1 flip

Stage 1 goes live with the **narrowest possible surface**: internal breakout
strategy only, no external signals, no limit-order escalation, no
rebalancing, at half caps.

**Exact env diff** (edit `config/risk.config`, do not touch anything outside
it — CLAUDE.md: risk limits live in `config/` only):

```diff
- RISK_TRADING_ENV=PAPER
+ RISK_TRADING_ENV=LIVE

+ RISK_SIGNALS_ENABLED=0            # webhook/file-drop inbox OFF — internal strategy only
+ RISK_LIMIT_ORDERS_ENABLED=0       # (already the default) no capped-limit escalation yet
+ RISK_REBALANCE_ENABLED=0          # (already the default) no midday rebalancing yet
```

Also: leave `RISK_ALLOWED_OVERLAYS` empty (the default — confirm nothing was
left set from a prior test) and confirm no rebalance target-weight snapshot
is fresh enough to matter — `latest_target_weights()` must return `None` or
a stale-enough row so `rebalance()` short-circuits even if
`RISK_REBALANCE_ENABLED` were flipped by accident. Confirm with `sqlite3
~/.autotrader.db "SELECT as_of_date, ingested_at FROM target_weights ORDER
BY ingested_at DESC LIMIT 1;"` returning nothing, or a row older than
`RISK_TARGET_STALENESS_HOURS`.

**First-session reduced caps** — halve the notional/position/loss caps you'd
otherwise run at. Concrete example (adjust to your actual live account size —
these numbers assume the same $2000/100-share/​$500/$1000 paper defaults
in `config/risk.config.example`):

| Variable | Current (paper) default | Stage-1 live (halved) example |
|---|---|---|
| `RISK_MAX_ORDER_NOTIONAL` | `2000` | `1000` |
| `RISK_MAX_POSITION_QTY` | `100` | `50` |
| `RISK_DAILY_LOSS_LIMIT` | `500` | `250` |
| `RISK_DAILY_LOSS_HALT` | `1000` | `500` |

These are risk-limit values — changing them requires the explicit human
review CLAUDE.md mandates. Do not carry this halving forward into Stage 2/3
without a fresh review; it is a first-session-only precaution, re-evaluated
at each promotion (§6).

**Manual GUI trade unlock.** Once OpenD is pointed at the live account (not
the SIMULATE one), unlock trading **by hand in the OpenD GUI**. AutoTrader
never calls `unlock_trade` / `TrdUnlockTrade` and must not — this is a hard
CLAUDE.md rule, not a v1-only restriction. If unlock is not done, every order
placement will be rejected by OpenD itself (a safe failure, but confirm the
unlock is done before expecting fills).

Start the trader normally (`RUNBOOK.md` §4) and confirm the startup log
reads `TRADING_ENV=LIVE (paper-only v1)` — yes, that log line's static
`(paper-only v1)` suffix is stale/misleading once you're live; do not let it
cause a false sense that the guard rejected you. What matters is the gate
did **not** refuse to start (no `"v1 is paper-only; refusing to start"`
error) and the account snapshot's account id resolves to the live account
(`FUTU_ACC_ID`), not the paper one.

---

## 4. Kill-switch test

During the **first live session**, with a single, tiny position on (the
smallest size your Stage-1 caps allow — one share if that clears
`RISK_MAX_ORDER_NOTIONAL`):

1. Let the breakout strategy or a manual test enter one position (with its
   trailing stop attached — confirm via `sqlite3 ~/.autotrader.db "SELECT
   side, qty, order_type, state FROM trades ORDER BY id DESC LIMIT 5;"`).
2. **Ctrl-C the trader process.** This runs `TradeEngine.shutdown()` ->
   `cancel_working_orders()` -> (SOLE) `broker.cancel_all()`. Confirm in the
   **OpenD GUI itself** (not just the log line) that the working trailing
   stop order for that position is gone from the open-orders list. A log
   line claiming `"shutdown: cancel_all completed"` is necessary but not
   sufficient — visually confirm in the GUI.
3. Restart the trader. On `ENTRY_OPEN`'s stop reconcile (or the next
   `RISK_CHECK_MID`/`RISK_CHECK_LATE`/`RISK_SWEEP` backstop),
   `StopManager.reconcile` should re-attach a fresh trailing stop for the
   still-held position — confirm the "stop reconcile: attached=1" log line
   and a new order in the GUI.

**Supervised-mode stop.** If the trader is running under launchd
(`deploy/com.autotrader.trader.plist`, `KeepAlive`+`RunAtLoad`), Ctrl-C does
not apply — launchd will simply restart the process. The supervised-mode
equivalent of the kill-switch test, and the correct way to stop the trader
for any planned maintenance while under supervision, is:

```bash
launchctl unload ~/Library/LaunchAgents/com.autotrader.trader.plist
```

`unload` (unlike killing the PID) tells launchd not to restart it, and the
process still runs its normal shutdown path (cancel-on-shutdown) before
exiting — confirm the same GUI check as step 2 above. Reload with
`launchctl load -w ~/Library/LaunchAgents/com.autotrader.trader.plist` (see
`deploy/README.md` §3).

---

## 5. Rollback

If Stage 1 needs to be reversed (bad fill behavior, a caught bug, anything
that erodes confidence):

1. Flip `RISK_TRADING_ENV=LIVE` back to `RISK_TRADING_ENV=PAPER` in
   `config/risk.config`.
2. Restart the trader (Ctrl-C or `launchctl unload`/`load`, per how it's
   running).
3. **Verify** the startup log reads `TRADING_ENV=PAPER (paper-only v1)` — do
   not trust that the config file was edited correctly; read the actual
   process log.
4. If any live orders are still working when you roll back: they do **not**
   auto-cancel just because the config flips (a config edit doesn't reach
   into OpenD) — cancel them **manually in the OpenD GUI**. `cancel_all()`
   only runs on this process's own shutdown/HALT/EOD paths, and after
   rollback the process is (intentionally) back to PAPER and has no live
   broker handle to reach the LIVE account's orders.
5. If the SQLite projection looks suspect (a partial write, a crash mid-fill,
   numbers that don't reconcile against the OpenD GUI's own view of the live
   account) — **restore `~/.autotrader.db` from the last verified backup**
   (`~/.autotrader_backups/autotrader-<date>.db`, per §1's restore drill)
   rather than trying to hand-patch the live projection. Stop the trader
   first, copy the backup over the live path, then restart.

---

## 6. Stage promotion criteria

A **clean session** is a full trading day where ALL of the following hold:

- No **Critical**-severity alert fired (`AlertSink.send` at a halt/critical
  key — check Slack history and `sqlite3 ~/.autotrader.db "SELECT * FROM
  halts ORDER BY id DESC;"` for that date).
- No `HALTED_UNHEALTHY` episode (`SessionRunner.run_once` returning that
  state — check trader logs for `"HALTED_UNHEALTHY"`).
- The EOD Slack report was delivered (`EOD_REPORT` job at 16:30 ET —
  confirm the message landed, not just that the job ran).
- Stops were attached for **every** entry that session — check the stop
  reconcile log line (`"stop reconcile: attached=%d failed=%d orphans=%d
  protected=%d skipped=%d"`) after `ENTRY_OPEN`/`RISK_CHECK_MID`/
  `RISK_CHECK_LATE`/`RISK_SWEEP` and confirm `failed=0` and `skipped=0` for
  every entry made that day.
- **Zero foreign-order incidents** — no `"stop reconcile: cancelled orphan
  order..."` or `"stop reconcile: foreign order ... left untouched"` log
  line referencing an order this session didn't place itself (relevant once
  `RISK_ACCOUNT_OWNERSHIP=SHARED` re-enters the picture at Stage 2/3 — see §7).

**Promotion gates:**

- **Stage 1 -> Stage 2**: after **5** clean sessions.
- **Stage 2 -> Stage 3**: after **5 more** clean sessions, **plus** a logged
  rebalance dry-run: with `RISK_REBALANCE_ENABLED` still `0`, invoke
  `autotrader.rebalance.compute_plan(snapshot, scores, prices, cfg)` directly
  against a real live-account snapshot and the latest ingested target
  weights (bypassing `TradeEngine.rebalance()`'s enabled-gate, which would
  otherwise short-circuit before computing anything) and log the resulting
  `RebalancePlan.trades` / `.skipped` tuples. This exercises the drift-band
  math against real numbers with **zero orders submitted** — `compute_plan`
  is a pure function (`autotrader/rebalance.py`); nothing about calling it
  directly touches the broker. Only after inspecting that log and being
  satisfied the plan looks sane does Stage 3 flip `RISK_REBALANCE_ENABLED=1`
  (alongside `RISK_LIMIT_ORDERS_ENABLED=1` and `RISK_SIGNALS_ENABLED=1`, each
  reviewed independently) and restore full (non-halved) caps.

A session with any open question ("did that alert actually fire, or did I
just see the log line") does not count as clean — resolve the ambiguity
before counting it.

---

## 7. SNP-bot coexistence check

AutoTrader shares its Moomoo account and OpenD instance with an independent
process — the SNP trading bot (`com.bot.trading`, a separate repo, not part
of this codebase). Post-flip, for at least the first live session:

- Confirm **both** processes are healthy: `launchctl list | grep com.bot`
  and `launchctl list | grep com.autotrader` both show a live PID with a
  clean last-exit status.
- Confirm **AutoTrader is on the LIVE account** (`FUTU_ACC_ID` resolves to
  the live account id) and **the SNP bot is still on its SIMULATE account**
  — these must never be the same account id once AutoTrader goes live; cross
  check both processes' configured account ids against `get_accounts.py`'s
  output before assuming.
- Watch **OpenD quote-quota headroom** for the session: both processes pull
  quotes/snapshots against the same OpenD instance's rate limits
  (`skills/moomooapi/docs/API_LIMITS.md`). AutoTrader's own
  `AUTOTRADER_SNAPSHOT_CACHE_TICKS` (default `6`, ~30s at the 5s loop
  interval) exists specifically to keep its `get_account()` calls (2 refresh
  tokens each, against a 10-per-30s budget) from starving the SNP bot's own
  queries — confirm it is still set to at least `6` and that neither
  process is logging quota-rejection errors during the session.

Since Stage 1 runs `RISK_ACCOUNT_OWNERSHIP=SOLE` (per §1's preconditions —
the SNP bot is stopped for the SOLE rehearsal, but note **SOLE is also the
mode used for the actual Stage 1 live flip itself**, per §2/§3), the SHARED
scoping logic (`StopManager.reconcile`'s orphan/attach scoping,
`TradeEngine.cancel_working_orders()`'s `cancel_tracked_orders` path,
`_flatten_all`'s non-owned skip, `ground_truth_sync(owned_only=True)`) is
**not yet exercised live** at Stage 1. If/when the SNP bot resumes running
against the same live account (making `RISK_ACCOUNT_OWNERSHIP=SHARED`
required — recall `RISK_TRADING_ENV=LIVE` + `SHARED` together is refused at
config-load, so this needs its own deliberate, human-reviewed decision, not
a silent flip), re-run this coexistence check with SHARED-mode logs
specifically in view: every reconcile/cancel/flatten log line should name
which symbols/orders were scoped in vs. left alone, and none of the SNP
bot's own positions or orders should ever appear in AutoTrader's cancel or
flatten activity.
