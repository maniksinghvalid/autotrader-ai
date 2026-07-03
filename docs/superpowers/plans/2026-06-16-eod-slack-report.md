# EOD Slack Report Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** At 16:30 EST each trading day, post a session summary (P&L header, the day's activity with the driving signal per trade, and open positions) to the Slack channel `#portfolio_updates` via an incoming webhook.

**Architecture:** A new pure module `autotrader/reporting/eod_reporter.py` reads only from the existing SQLite projection (`autotrader/db.py`) — no Moomoo SDK import, no broker handle — and POSTs a Slack Block Kit message over stdlib `urllib`. A new `EOD_REPORT` scheduler job at 16:30 dispatches it through `SessionRunner`. It is opt-in: with no webhook URL configured, the job is a no-op and the runner is unchanged. A reporting/network failure is retried briefly, then logged — it never raises into the trading loop.

**Tech Stack:** Python 3, stdlib `urllib.request` + `json` (no new dependencies), SQLite (`autotrader.db.DB`), pytest with `FixedClock`/`SimBroker`, Slack incoming webhooks (Block Kit).

---

## Context the implementer needs

- **Read these first:** `autotrader/db.py` (table schemas + `DB._conn`), `autotrader/scheduler.py`, `autotrader/runner.py`, `autotrader/main.py:401-414`, `tests/test_runner.py:1-50`, `tests/test_scheduler.py`.
- **Run tests with:** `python3 -m pytest -q` from the repo root. The suite currently passes (~231 tests, 2 skipped). Do **not** introduce new skips.
- **DB access pattern:** tests and code read the projection directly via `db._conn.execute(...).fetchall()` (see `tests/test_runner.py:59`). The reporter does the same — it does not need new `DB` methods.
- **Key schema facts** (from `autotrader/db.py`):
  - `performance(date, day_pnl, total_assets, cash, gross_exposure, updated_at)` — `date` is `YYYY-MM-DD`, one row per day.
  - `fills(fill_id, ts, symbol, side, qty, price)` — `ts` is an ISO timestamp; `side` ∈ `BUY`/`SELL`; may be multiple partial fills per symbol+side.
  - `signals(id, ts, symbol, direction, confidence, rationale, signal_id)` — `direction` ∈ `BUY`/`SELL`.
  - `positions(symbol, qty, avg_price, updated_at)` — latest snapshot per symbol.
- **Signal→trade linkage:** for each fill group `(symbol, side)`, the driving signal is the most recent `signals` row with the same `symbol`, `direction == side`, and same date. If none, the trade renders without signal detail (graceful).
- **Date filtering:** the report uses `now.date().isoformat()` (EST) and filters fills/signals by `substr(ts,1,10)`. At 16:30 EST the UTC date in `ts` equals the EST date, so this is correct in production; tests seed rows with explicit dates to stay deterministic.
- **`gross_exposure` is a dollar notional** (`AccountSnapshot.gross_exposure()` = Σ|qty|·avg_price). The report shows it as a percent of `total_assets`.

---

## File structure

- **Create** `autotrader/reporting/__init__.py` — empty package marker.
- **Create** `autotrader/reporting/eod_reporter.py` — `EODReporter` class, `ReportData`/`TradeLine`/`SignalRef` dataclasses, `_default_post`. Single responsibility: gather session data → render Slack payload → POST with retry. No SDK, no broker.
- **Modify** `autotrader/scheduler.py` — add the `EOD_REPORT` constant and a `(EOD_REPORT, time(16, 30))` schedule entry.
- **Modify** `autotrader/runner.py` — accept an optional `reporter`, dispatch the `EOD_REPORT` job.
- **Modify** `autotrader/main.py` — construct an `EODReporter` from `AUTOTRADER_SLACK_WEBHOOK_URL` and pass it to `SessionRunner` (opt-in).
- **Modify** `config/secure.config.example` — document `AUTOTRADER_SLACK_WEBHOOK_URL`.
- **Create** `tests/test_eod_reporter.py` — unit tests for gather/render/post.
- **Modify** `tests/test_scheduler.py` — assert `EOD_REPORT` fires at 16:30.
- **Modify** `tests/test_runner.py` — assert the runner dispatches to the reporter (and no-ops without one).

---

## Task 1: Reporting package + data gathering

**Files:**
- Create: `autotrader/reporting/__init__.py`
- Create: `autotrader/reporting/eod_reporter.py`
- Test: `tests/test_eod_reporter.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_eod_reporter.py`:

```python
"""EODReporter reads the SQLite projection (no SDK, no broker) and posts a
Slack summary. Tests seed the DB directly with controlled dates, inject a fake
HTTP poster, and drive with a fixed NY datetime."""
from datetime import datetime
from zoneinfo import ZoneInfo

from autotrader.db import DB
from autotrader.reporting.eod_reporter import EODReporter

_NY = ZoneInfo("America/New_York")
_DAY = "2026-06-16"


def _now():
    return datetime(2026, 6, 16, 16, 30, tzinfo=_NY)


def _seed(db, *, with_signal=True):
    db._conn.execute(
        "INSERT INTO performance (date,day_pnl,total_assets,cash,gross_exposure,updated_at) "
        "VALUES (?,?,?,?,?,?)",
        (_DAY, 842.13, 104712.0, 38204.0, 66000.0, _DAY + "T20:30:00+00:00"))
    db._conn.execute(
        "INSERT INTO fills (fill_id,ts,symbol,side,qty,price) VALUES (?,?,?,?,?,?)",
        ("f1", _DAY + "T14:00:00+00:00", "US.AAPL", "BUY", 10, 198.00))
    db._conn.execute(
        "INSERT INTO fills (fill_id,ts,symbol,side,qty,price) VALUES (?,?,?,?,?,?)",
        ("f2", _DAY + "T14:05:00+00:00", "US.AAPL", "BUY", 6, 199.00))
    if with_signal:
        db._conn.execute(
            "INSERT INTO signals (ts,symbol,direction,confidence,rationale,signal_id) "
            "VALUES (?,?,?,?,?,?)",
            (_DAY + "T13:59:00+00:00", "US.AAPL", "BUY", 0.82, "momentum breakout", "sig-1"))
    db._conn.execute(
        "INSERT INTO positions (symbol,qty,avg_price,updated_at) VALUES (?,?,?,?)",
        ("US.AAPL", 16, 198.40, _DAY + "T20:30:00+00:00"))
    db._conn.commit()


def _reporter(db, post):
    return EODReporter(db=db, webhook_url="https://hooks.slack.test/x",
                       trading_env="PAPER", http_post=post,
                       retries=3, backoff=lambda s: None)


def test_gather_aggregates_fills_and_links_signal(tmp_path):
    db = DB(str(tmp_path / "r.db"))
    _seed(db)
    r = _reporter(db, lambda url, payload: 200)
    data = r._gather(_now())
    assert len(data.activity) == 1
    line = data.activity[0]
    assert line.side == "BUY" and line.symbol == "US.AAPL"
    assert line.qty == 16                      # 10 + 6 summed
    assert abs(line.avg_price - 198.375) < 1e-6  # qty-weighted: (10*198 + 6*199)/16
    assert line.signal is not None
    assert abs(line.signal.confidence - 0.82) < 1e-9
    assert line.signal.rationale == "momentum breakout"
    assert data.positions == (("US.AAPL", 16),)
    assert data.day_pnl == 842.13 and data.total_assets == 104712.0
    db.close()


def test_gather_handles_missing_signal(tmp_path):
    db = DB(str(tmp_path / "r.db"))
    _seed(db, with_signal=False)
    r = _reporter(db, lambda url, payload: 200)
    data = r._gather(_now())
    assert len(data.activity) == 1
    assert data.activity[0].signal is None
    db.close()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_eod_reporter.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'autotrader.reporting'`.

- [ ] **Step 3: Create the package marker**

Create `autotrader/reporting/__init__.py` with a single line:

```python
"""Outbound reporting (Slack EOD summary). SDK-free: reads the DB projection only."""
```

- [ ] **Step 4: Implement the reporter module (dataclasses + `_gather`)**

Create `autotrader/reporting/eod_reporter.py`:

```python
"""EODReporter — posts an end-of-day session summary to Slack.

SDK-free and broker-free by design: it reads ONLY the SQLite projection
(autotrader.db.DB) and POSTs a Block Kit message over stdlib urllib. It has no
path to place or cancel orders (the mirror image of the inbound webhook's
privilege separation). A render or network failure is retried briefly then
logged — send_eod_report NEVER raises into the trading loop."""
from __future__ import annotations

import json
import logging
import time
import urllib.request
from dataclasses import dataclass
from datetime import datetime
from typing import Callable, Optional, Tuple

logger = logging.getLogger("autotrader.reporting")


@dataclass(frozen=True)
class SignalRef:
    direction: str
    confidence: float
    rationale: str


@dataclass(frozen=True)
class TradeLine:
    side: str
    symbol: str
    qty: float
    avg_price: float
    signal: Optional[SignalRef] = None


@dataclass(frozen=True)
class ReportData:
    date_label: str
    trading_env: str
    day_pnl: Optional[float]
    total_assets: Optional[float]
    cash: Optional[float]
    gross_exposure: Optional[float]
    activity: Tuple[TradeLine, ...]
    positions: Tuple[Tuple[str, int], ...]


def _default_post(url: str, payload: dict, *, timeout: float = 10.0) -> int:
    """POST the payload as JSON and return the HTTP status code."""
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # nosec - URL from config
        return resp.status


class EODReporter:
    def __init__(self, db, webhook_url: str, *, trading_env: str = "PAPER",
                 http_post: Callable[[str, dict], int] = _default_post,
                 retries: int = 3, backoff: Callable[[float], None] = time.sleep):
        self._db = db
        self._url = webhook_url
        self._env = trading_env
        self._post = http_post
        self._retries = retries
        self._sleep = backoff

    def _gather(self, now: datetime) -> ReportData:
        today = now.date().isoformat()
        c = self._db._conn
        perf = c.execute(
            "SELECT day_pnl, total_assets, cash, gross_exposure FROM performance "
            "WHERE date=?", (today,)).fetchone()
        fill_rows = c.execute(
            "SELECT symbol, side, SUM(qty) AS q, SUM(qty*price)/SUM(qty) AS avg_price "
            "FROM fills WHERE substr(ts,1,10)=? GROUP BY symbol, side "
            "ORDER BY symbol, side", (today,)).fetchall()
        activity = []
        for symbol, side, qty, avg_price in fill_rows:
            sig = c.execute(
                "SELECT direction, confidence, rationale FROM signals "
                "WHERE symbol=? AND direction=? AND substr(ts,1,10)=? "
                "ORDER BY id DESC LIMIT 1", (symbol, side, today)).fetchone()
            signal = SignalRef(sig[0], sig[1], sig[2]) if sig else None
            activity.append(TradeLine(side=side, symbol=symbol, qty=qty,
                                      avg_price=avg_price, signal=signal))
        positions = c.execute(
            "SELECT symbol, qty FROM positions WHERE qty != 0 ORDER BY symbol").fetchall()
        return ReportData(
            date_label=now.strftime("%a, %b %d %Y"),
            trading_env=self._env,
            day_pnl=perf[0] if perf else None,
            total_assets=perf[1] if perf else None,
            cash=perf[2] if perf else None,
            gross_exposure=perf[3] if perf else None,
            activity=tuple(activity),
            positions=tuple((r[0], r[1]) for r in positions),
        )
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_eod_reporter.py -q`
Expected: PASS (2 passed).

- [ ] **Step 6: Commit**

```bash
git add autotrader/reporting/__init__.py autotrader/reporting/eod_reporter.py tests/test_eod_reporter.py
git commit -m "feat(reporting): EODReporter data gathering from DB projection

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 2: Render the Slack payload

**Files:**
- Modify: `autotrader/reporting/eod_reporter.py`
- Test: `tests/test_eod_reporter.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_eod_reporter.py`:

```python
def test_render_full_report_text_and_blocks(tmp_path):
    db = DB(str(tmp_path / "r.db"))
    _seed(db)
    r = _reporter(db, lambda url, payload: 200)
    payload = r._render(r._gather(_now()))
    text = payload["text"]
    assert "Mon, Jun 16 2026" in text and "PAPER" in text
    assert "+842.13" in text                  # day P&L, signed
    assert "BUY US.AAPL" in text and "16" in text
    assert "198.38" in text or "198.37" in text  # weighted avg, 2dp
    assert "momentum breakout" in text and "0.82" in text
    assert "US.AAPL 16" in text               # open positions
    assert isinstance(payload["blocks"], list) and len(payload["blocks"]) >= 3
    assert payload["blocks"][0]["type"] == "header"
    db.close()


def test_render_quiet_day_heartbeat(tmp_path):
    db = DB(str(tmp_path / "r.db"))
    db._conn.execute(
        "INSERT INTO performance (date,day_pnl,total_assets,cash,gross_exposure,updated_at) "
        "VALUES (?,?,?,?,?,?)",
        (_DAY, 0.0, 100000.0, 100000.0, 0.0, _DAY + "T20:30:00+00:00"))
    db._conn.commit()
    r = _reporter(db, lambda url, payload: 200)
    payload = r._render(r._gather(_now()))
    assert "no trades today" in payload["text"].lower()
    assert "100,000" in payload["text"]       # P&L header still present
    db.close()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_eod_reporter.py -q`
Expected: FAIL — `AttributeError: 'EODReporter' object has no attribute '_render'`.

- [ ] **Step 3: Implement `_render`, `_build_blocks`, and the percent helpers**

Add these methods to the `EODReporter` class in `autotrader/reporting/eod_reporter.py` (e.g. directly after `_gather`):

```python
    @staticmethod
    def _pnl_pct(d: ReportData) -> float:
        base = (d.total_assets or 0.0) - (d.day_pnl or 0.0)
        return (d.day_pnl / base * 100) if base else 0.0

    @staticmethod
    def _gross_pct(d: ReportData) -> float:
        return (d.gross_exposure / d.total_assets * 100) if d.total_assets else 0.0

    def _render(self, d: ReportData) -> dict:
        lines = [f"AutoTrader session summary — {d.date_label} ({d.trading_env})"]
        if d.total_assets is not None:
            lines.append(
                f"Day P&L: {d.day_pnl:+,.2f} ({self._pnl_pct(d):+.2f}%) | "
                f"Assets: {d.total_assets:,.0f} | Cash: {d.cash:,.0f} | "
                f"Gross exp: {self._gross_pct(d):.0f}%")
        if d.activity:
            lines.append(f"Activity — {len(d.activity)} trade(s):")
            for t in d.activity:
                s = f"  {t.side} {t.symbol} x{int(t.qty)} @ {t.avg_price:,.2f}"
                if t.signal:
                    s += (f"  [signal {t.signal.direction} conf "
                          f"{t.signal.confidence:.2f}: {t.signal.rationale}]")
                lines.append(s)
        else:
            lines.append("Activity — no trades today")
        pos = " · ".join(f"{sym} {qty}" for sym, qty in d.positions) or "none"
        lines.append(f"Open positions ({len(d.positions)}): {pos}")
        return {"text": "\n".join(lines), "blocks": self._build_blocks(d)}

    def _build_blocks(self, d: ReportData) -> list:
        blocks = [{"type": "header", "text": {"type": "plain_text",
                   "text": f"Session summary — {d.date_label} ({d.trading_env})"}}]
        if d.total_assets is not None:
            blocks.append({"type": "section", "fields": [
                {"type": "mrkdwn", "text": f"*Day P&L*\n{d.day_pnl:+,.2f} ({self._pnl_pct(d):+.2f}%)"},
                {"type": "mrkdwn", "text": f"*Total assets*\n{d.total_assets:,.0f}"},
                {"type": "mrkdwn", "text": f"*Cash*\n{d.cash:,.0f}"},
                {"type": "mrkdwn", "text": f"*Gross exp.*\n{self._gross_pct(d):.0f}%"},
            ]})
        blocks.append({"type": "divider"})
        if d.activity:
            rows = []
            for t in d.activity:
                row = f"*{t.side} {t.symbol}* ×{int(t.qty)} @ {t.avg_price:,.2f}"
                if t.signal:
                    row += (f"\n_signal {t.signal.direction} · conf "
                            f"{t.signal.confidence:.2f} · {t.signal.rationale}_")
                rows.append(row)
            blocks.append({"type": "section", "text": {"type": "mrkdwn",
                           "text": f"*Activity — {len(d.activity)} trade(s)*\n" + "\n".join(rows)}})
        else:
            blocks.append({"type": "section", "text": {"type": "mrkdwn",
                           "text": "*Activity*\nNo trades today"}})
        pos = " · ".join(f"{sym} {qty}" for sym, qty in d.positions) or "none"
        blocks.append({"type": "section", "text": {"type": "mrkdwn",
                       "text": f"*Open positions ({len(d.positions)})*\n{pos}"}})
        return blocks
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_eod_reporter.py -q`
Expected: PASS (4 passed).

- [ ] **Step 5: Commit**

```bash
git add autotrader/reporting/eod_reporter.py tests/test_eod_reporter.py
git commit -m "feat(reporting): render Slack Block Kit payload + text fallback

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 3: Post with retry; `send_eod_report` entrypoint

**Files:**
- Modify: `autotrader/reporting/eod_reporter.py`
- Test: `tests/test_eod_reporter.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_eod_reporter.py`:

```python
class _Capture:
    """Fake http_post: records calls, fails the first `fail_times`, else returns status."""
    def __init__(self, status=200, fail_times=0):
        self.calls = []
        self._status = status
        self._fail_times = fail_times

    def __call__(self, url, payload):
        self.calls.append((url, payload))
        if len(self.calls) <= self._fail_times:
            raise OSError("boom")
        return self._status


def test_send_posts_once_on_success(tmp_path):
    db = DB(str(tmp_path / "r.db"))
    _seed(db)
    cap = _Capture(status=200)
    _reporter(db, cap).send_eod_report(_now())
    assert len(cap.calls) == 1
    assert cap.calls[0][0] == "https://hooks.slack.test/x"
    assert "blocks" in cap.calls[0][1]
    db.close()


def test_send_retries_then_succeeds(tmp_path):
    db = DB(str(tmp_path / "r.db"))
    _seed(db)
    cap = _Capture(status=200, fail_times=1)
    _reporter(db, cap).send_eod_report(_now())
    assert len(cap.calls) == 2          # one failure, one success
    db.close()


def test_send_failure_exhausts_retries_without_raising(tmp_path):
    db = DB(str(tmp_path / "r.db"))
    _seed(db)
    cap = _Capture(fail_times=99)
    _reporter(db, cap).send_eod_report(_now())   # must NOT raise
    assert len(cap.calls) == 3          # retries=3
    db.close()


def test_send_non_2xx_status_is_retried(tmp_path):
    db = DB(str(tmp_path / "r.db"))
    _seed(db)
    cap = _Capture(status=500)
    _reporter(db, cap).send_eod_report(_now())   # must NOT raise
    assert len(cap.calls) == 3
    db.close()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_eod_reporter.py -q`
Expected: FAIL — `AttributeError: 'EODReporter' object has no attribute 'send_eod_report'`.

- [ ] **Step 3: Implement `send_eod_report` and `_post_with_retry`**

Add these methods to the `EODReporter` class in `autotrader/reporting/eod_reporter.py`:

```python
    def send_eod_report(self, now: datetime) -> None:
        """Build and post the EOD summary. Never raises into the runner."""
        try:
            payload = self._render(self._gather(now))
        except Exception as e:
            logger.error("EOD report build failed: %s", e)
            return
        self._post_with_retry(payload)

    def _post_with_retry(self, payload: dict) -> None:
        last = None
        for attempt in range(1, self._retries + 1):
            try:
                status = self._post(self._url, payload)
                if 200 <= status < 300:
                    logger.info("EOD report posted to Slack (status %s)", status)
                    return
                last = f"HTTP {status}"
            except Exception as e:  # network/URL errors — retry, then give up
                last = str(e)
            if attempt < self._retries:
                self._sleep(min(2 ** (attempt - 1), 5))
        logger.error("EOD report POST failed after %d attempt(s): %s",
                     self._retries, last)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_eod_reporter.py -q`
Expected: PASS (8 passed).

- [ ] **Step 5: Commit**

```bash
git add autotrader/reporting/eod_reporter.py tests/test_eod_reporter.py
git commit -m "feat(reporting): send_eod_report with bounded retry, never raises

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 4: Scheduler `EOD_REPORT` job at 16:30

**Files:**
- Modify: `autotrader/scheduler.py:17` and `autotrader/scheduler.py:27`
- Test: `tests/test_scheduler.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_scheduler.py`:

```python
def test_eod_report_fires_after_1630_and_once_per_day():
    from autotrader.scheduler import LifecycleScheduler, EOD_REPORT
    s = LifecycleScheduler()
    s.poll(_dt(16, 20))                     # nothing new at 16:20 beyond catch-up
    due = s.poll(_dt(16, 31))
    assert EOD_REPORT in due
    assert s.poll(_dt(16, 45)) == []        # does not re-fire same day


def test_eod_report_fires_after_eod_flatten():
    from autotrader.scheduler import LifecycleScheduler, EOD_FLATTEN, EOD_REPORT
    s = LifecycleScheduler()
    due = s.poll(_dt(16, 31))               # first poll catches up both, in order
    assert due.index(EOD_FLATTEN) < due.index(EOD_REPORT)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_scheduler.py -q`
Expected: FAIL — `ImportError: cannot import name 'EOD_REPORT'`.

- [ ] **Step 3: Add the constant**

In `autotrader/scheduler.py`, add this line immediately after line 17 (`RISK_CHECK_LATE = ...`):

```python
EOD_REPORT = "EOD_REPORT"         # 16:30 — post session summary to Slack
```

- [ ] **Step 4: Add the schedule entry**

In `autotrader/scheduler.py`, add this line to the `_SCHEDULE` tuple immediately after the `(EOD_FLATTEN, time(16, 15)),` entry (currently line 27), keeping chronological order:

```python
    (EOD_REPORT, time(16, 30)),
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_scheduler.py -q`
Expected: PASS (all scheduler tests).

- [ ] **Step 6: Commit**

```bash
git add autotrader/scheduler.py tests/test_scheduler.py
git commit -m "feat(scheduler): EOD_REPORT job at 16:30 after EOD_FLATTEN

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 5: Runner dispatch to the reporter

**Files:**
- Modify: `autotrader/runner.py:20-23` (import), `autotrader/runner.py:29-42` (constructor), `autotrader/runner.py:66-71` (dispatch)
- Test: `tests/test_runner.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_runner.py`:

```python
class _FakeReporter:
    def __init__(self):
        self.calls = []

    def send_eod_report(self, now):
        self.calls.append(now)


def test_eod_report_job_invokes_reporter(tmp_path):
    b = SimBroker(quotes={"US.AAPL": 101.0}, cash=100000.0)
    rep = _FakeReporter()
    runner, db, gate = _build(tmp_path, b, reporter=rep)
    runner.run_once(_dt(16, 31))            # first poll catches up through EOD_REPORT
    assert len(rep.calls) == 1
    assert rep.calls[0] == _dt(16, 31)
    db.close()


def test_eod_report_without_reporter_is_noop(tmp_path):
    b = SimBroker(quotes={"US.AAPL": 101.0}, cash=100000.0)
    runner, db, gate = _build(tmp_path, b)   # reporter defaults to None
    runner.run_once(_dt(16, 31))            # must not raise
    db.close()
```

Then update the `_build` helper in `tests/test_runner.py` (currently lines 35-49) to accept and pass a reporter. Change its signature and the `SessionRunner(...)` call:

```python
def _build(tmp_path, broker, *, healthy=True, gate_enabled=False, inbox=None, reporter=None):
    db = DB(str(tmp_path / "runner.db"))
    gate = EntryGate(enabled=gate_enabled)
    strat = ThresholdStrategy(StrategyParams(symbol="US.AAPL", entry_price=100.0,
                                             stop_loss_pct=0.05, take_profit_pct=0.10,
                                             confidence=0.7))
    eng = TradeEngine(broker=broker, strategy=strat, cfg=_cfg(), order_qty=10,
                      audit_path=str(tmp_path / "audit.jsonl"), db=db, entry_gate=gate)
    watch = Watchdog(health_check=lambda: healthy, reconcile=lambda: None,
                     sleep=lambda s: None)
    runner = SessionRunner(engine=eng, broker=broker, db=db, gate=gate,
                           scheduler=LifecycleScheduler(), watchdog=watch,
                           clock=FixedClock(_dt(8, 0)), sleep=lambda s: None,
                           signal_inbox=inbox, reporter=reporter)
    return runner, db, gate
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_runner.py -q`
Expected: FAIL — `TypeError: __init__() got an unexpected keyword argument 'reporter'`.

- [ ] **Step 3: Add `EOD_REPORT` to the runner import**

In `autotrader/runner.py`, update the scheduler import (lines 20-23) to include `EOD_REPORT`:

```python
from autotrader.scheduler import (
    LifecycleScheduler, PRE_OPEN_SYNC, ENTRY_OPEN, RISK_SWEEP, EOD_FLATTEN,
    REBALANCE, RISK_CHECK_MID, RISK_CHECK_LATE, EOD_REPORT,
)
```

- [ ] **Step 4: Accept the reporter in the constructor**

In `autotrader/runner.py`, change the `SessionRunner.__init__` signature (line 31-32) to add `reporter=None`, and store it. The signature line becomes:

```python
                 sleep: Callable[[float], None], loop_interval: float = 5.0,
                 signal_inbox=None, reporter=None):
```

And add this assignment after `self._inbox = signal_inbox` (line 42):

```python
        self._reporter = reporter
```

- [ ] **Step 5: Dispatch the job**

In `autotrader/runner.py`, add this branch to `_run_job` immediately after the `EOD_FLATTEN` branch (after line 71):

```python
        elif job == EOD_REPORT:
            if self._reporter is not None:
                self._reporter.send_eod_report(now)
                logger.info("EOD_REPORT: session summary sent to Slack")
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_runner.py -q`
Expected: PASS (all runner tests).

- [ ] **Step 7: Commit**

```bash
git add autotrader/runner.py tests/test_runner.py
git commit -m "feat(runner): dispatch EOD_REPORT job to the reporter (opt-in)

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 6: Wire `main.py` from config (manual/live path)

**Files:**
- Modify: `autotrader/main.py:401-414`
- Modify: `config/secure.config.example`

> `main()` is annotated `# pragma: no cover` (live entrypoint). There is no unit test for this task; verification is by import + a config-disabled smoke check in Step 4.

- [ ] **Step 1: Construct the reporter from env, opt-in**

In `autotrader/main.py`, immediately after the inbox-wiring block (after line 407, the `logger.info("external-signal inbox at %s", inbox_dir)` line) and before `runner = SessionRunner(`, insert:

```python
    reporter = None
    slack_url = os.getenv("AUTOTRADER_SLACK_WEBHOOK_URL")
    if slack_url:
        from autotrader.reporting.eod_reporter import EODReporter
        reporter = EODReporter(db=db, webhook_url=slack_url,
                               trading_env=cfg.trading_env)
        logger.info("EOD Slack reporter enabled")
```

- [ ] **Step 2: Pass it to the runner**

In `autotrader/main.py`, update the `SessionRunner(...)` constructor call (lines 408-414) to pass `reporter=reporter`. The call becomes:

```python
    runner = SessionRunner(
        engine=engine, broker=broker, db=db, gate=gate,
        scheduler=LifecycleScheduler(), watchdog=watchdog, clock=Clock(),
        sleep=time.sleep,
        loop_interval=float(os.getenv("AUTOTRADER_LOOP_INTERVAL", "5")),
        signal_inbox=inbox,
        reporter=reporter,
    )
```

- [ ] **Step 3: Document the config key**

In `config/secure.config.example`, append:

```
# Slack incoming-webhook URL for the end-of-day session report (posts to
# #portfolio_updates). Leave unset to disable EOD reporting entirely (the
# runner behaves exactly as without it). Create one at:
#   https://api.slack.com/messaging/webhooks
AUTOTRADER_SLACK_WEBHOOK_URL=
```

- [ ] **Step 4: Verify the module imports and main.py parses**

Run: `python3 -c "import autotrader.main; from autotrader.reporting.eod_reporter import EODReporter; print('ok')"`
Expected: prints `ok` with no ImportError.

- [ ] **Step 5: Commit**

```bash
git add autotrader/main.py config/secure.config.example
git commit -m "feat(main): wire opt-in EOD Slack reporter via AUTOTRADER_SLACK_WEBHOOK_URL

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 7: Full-suite verification

**Files:** none (verification only)

- [ ] **Step 1: Run the whole offline suite**

Run: `python3 -m pytest -q`
Expected: all tests pass; the skipped count is unchanged from baseline (2). The new `tests/test_eod_reporter.py` (8 tests) plus the added scheduler/runner tests are green. If anything fails, fix it before continuing — do not skip.

- [ ] **Step 2: Confirm no SDK leaked into the reporter**

Run: `python3 -c "import sys; import autotrader.reporting.eod_reporter as m; assert 'moomoo' not in sys.modules, 'SDK leaked'; print('SDK-free OK')"`
Expected: prints `SDK-free OK`.

- [ ] **Step 3: Final commit (if any uncommitted verification fixups)**

```bash
git add -A
git commit -m "test(reporting): verify EOD report suite green and SDK-free

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

(Skip this commit if the working tree is clean.)

---

## Manual verification (post-merge, human-driven — not part of the automated plan)

These require a real Slack workspace and cannot run headlessly:

1. Create an incoming webhook bound to `#portfolio_updates`; put the URL in `config/secure.config` as `AUTOTRADER_SLACK_WEBHOOK_URL`.
2. Source the config and run the trader; confirm a formatted message lands in `#portfolio_updates` at 16:30 EST (or trigger an out-of-hours smoke by temporarily polling past 16:30 with seeded DB rows).
3. Confirm the message matches the agreed layout: P&L header, activity with per-trade signal, open positions; no risk/halts section.

---

## Self-review notes

- **Spec coverage:** P&L header → Task 2 `_build_blocks`; Activity + signal linkage → Tasks 1–2; open positions → Tasks 1–2; risk/halts excluded → not rendered; 16:30 scheduled job → Task 4; incoming-webhook delivery → Tasks 3/6; SDK-free reader → Task 1 + Task 7 Step 2; never-halts/retry → Task 3; opt-in config + `secure.config` → Task 6; quiet-day heartbeat → Task 2 test; tests mirror `test_runner`/`test_webhook` → throughout.
- **Type consistency:** `EODReporter(db, webhook_url, *, trading_env, http_post, retries, backoff)`, `send_eod_report(now)`, `_gather(now) -> ReportData`, `_render(ReportData) -> dict`, `_post_with_retry(dict)`, and the `ReportData`/`TradeLine`/`SignalRef` field names are used identically across all tasks. `http_post` signature `(url, payload) -> int` matches `_default_post`, `_Capture`, and the inline lambdas. Runner kwarg `reporter=` matches `main.py` and the `_build` helper.
- **No placeholders:** every code/test/command step is concrete.
