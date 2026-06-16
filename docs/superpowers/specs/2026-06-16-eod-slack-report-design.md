# EOD Slack Report — Design Spec

**Date:** 2026-06-16
**Branch:** feat/autotrader-paper-v1
**Status:** Approved (design); pending implementation plan

## Goal

At the end of each trading session, post a summary of the day to the Slack
channel `#portfolio_updates`. The report covers the account P&L snapshot, the
day's trading activity (with the signal that drove each trade), and end-of-day
open positions. It always posts on a trading day as a heartbeat, even when no
trades occurred.

## Constraints (inherited from CLAUDE.md)

- **No Moomoo SDK import** in the reporter and **no broker handle** — it reads
  only from the existing SQLite DB. This keeps it on the SDK-free side of the
  core/SDK separation.
- **Never halts trading.** A reporting or network failure must never raise into
  the runner or interfere with the order path.
- **No hardcoded secrets/hosts.** The Slack webhook URL comes from config
  (`config/secure.config`, gitignored), not source.
- **Outbound only, no order path.** The reporter can read state and talk to
  Slack; it has no ability to place or cancel orders. This is the mirror image
  of the inbound webhook's privilege separation.

## Report content (agreed)

1. **P&L header** — four metric cards: day P&L (and %), total assets, cash,
   gross exposure.
2. **Activity** — for each of the day's trades (buys and sells): side, symbol,
   quantity, fill price, and the **driving signal** (direction, confidence,
   rationale) joined from the `signals` table.
3. **Open positions** — end-of-day holdings (symbol + quantity).

The risk/halts section is intentionally **excluded**.

On a quiet day (no fills or orders), the report still posts (heartbeat) showing
"No trades today" plus the P&L header and positions snapshot.

## Architecture

### New module: `autotrader/reporting/eod_reporter.py`

A pure module (no SDK, no broker). Class:

```
EODReporter(
    db,                       # autotrader.db.DB
    webhook_url,              # str — Slack incoming webhook
    *,
    trading_env="PAPER",      # label shown in the report header
    http_post=requests.post,  # injectable for tests
    retries=3,
)
```

Public method:

- `send_eod_report(now: datetime) -> None` — the job entrypoint. Gathers data,
  renders the Slack payload, posts it. Never raises.

Internal helpers:

- `_gather(now) -> ReportData` — runs the DB queries and assembles a plain
  data object (dataclass) of everything the report needs.
- `_render(data) -> dict` — builds the Slack **Block Kit** JSON payload.
- `_post(payload) -> None` — POSTs with brief retry/backoff; on total failure
  logs at ERROR and returns.

### Data sources (all from `autotrader/db.py`)

- `performance` (today's row): `day_pnl, total_assets, cash, gross_exposure`.
  Refreshed during the session at `RISK_SWEEP` (15:30) and `EOD_FLATTEN`
  (16:15), so it is current by 16:30.
- `fills` / `trades` (today): the executed activity.
- `signals` (today): `symbol, direction, confidence, rationale, signal_id` —
  joined to each traded symbol.
- `positions` (latest snapshot): end-of-day holdings.

### Signal → trade linkage

Each trade is associated with the driving signal **by symbol within the same
calendar day**, choosing the most recent acted-on signal for that symbol. If no
matching signal row is found, the trade is shown without signal detail (graceful
degradation, not an error).

### Slack payload format

**Block Kit JSON** (`blocks` array) for the rich card layout, with a top-level
`text` fallback for notifications/accessibility. The webhook is bound to
`#portfolio_updates` at creation time, so no channel field is sent.

## Scheduler & runner wiring

### `autotrader/scheduler.py`

- Add constant `EOD_REPORT = "EOD_REPORT"`.
- Add `(EOD_REPORT, time(16, 30))` to `_SCHEDULE`, after `EOD_FLATTEN`.
- Idempotent per calendar day, consistent with all other jobs.

### `autotrader/runner.py`

- Inject an optional `reporter` into `SessionRunner.__init__`; store as
  `self._reporter`.
- Dispatch in `_run_job`:

  ```
  elif job == EOD_REPORT:
      if self._reporter is not None:
          self._reporter.send_eod_report(now)
          logger.info("EOD_REPORT: session summary sent to Slack")
  ```

### `autotrader/main.py`

- Read `AUTOTRADER_SLACK_WEBHOOK_URL` from env/config.
- If present: construct `EODReporter(db=..., webhook_url=..., trading_env=...)`
  and pass it to `SessionRunner(reporter=...)`.
- If absent: `reporter=None` → the EOD_REPORT job is a no-op and the runner
  behaves exactly as today. The feature is opt-in via config.

## Config

- New key `AUTOTRADER_SLACK_WEBHOOK_URL` in `config/secure.config` (gitignored).
- Add a documented placeholder to `config/secure.config.example`.
- No channel/token config — incoming webhook handles channel binding.

## Error handling

- `send_eod_report` is wrapped so it **never propagates** exceptions to the
  runner.
- `_post` attempts the POST up to `retries` times with short backoff. On final
  failure it logs at ERROR with context (status code / exception) and returns.
- DB-read or render errors are caught, logged, and swallowed — the runner
  continues its shutdown/loop normally.

## Testing

New `tests/test_eod_reporter.py`, following the `test_runner.py` /
`test_webhook.py` conventions (seed `DB(tmp_path)`, inject a fake `http_post`,
drive with `FixedClock`):

- Full report renders correct P&L, activity, and positions fields.
- Signal → trade linkage maps each trade to its driving signal; trades with no
  matching signal render without signal detail.
- Quiet-day heartbeat: zero activity still posts, showing "No trades today" plus
  the P&L header and positions.
- POST failure: retries `retries` times, then logs and does **not** raise.
- `reporter=None` wiring in the runner is a no-op.
- Scheduler: `EOD_REPORT` is due at 16:30 EST and fires at most once per day.

## Out of scope

- Bot-token / multi-channel delivery (incoming webhook only).
- Risk/halts section.
- Charts/attachments/threading.
- Backfill of historical sessions.
