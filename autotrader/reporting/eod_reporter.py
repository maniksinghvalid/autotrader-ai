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
class DriverRef:
    """Non-signal attribution for a trade (e.g. a rebalance trim/top-up), so a
    trade with no driving Signal is still explained in the report."""
    kind: str
    detail: str


@dataclass(frozen=True)
class TradeLine:
    side: str
    symbol: str
    qty: float
    avg_price: float
    signal: Optional[SignalRef] = None
    driver: Optional[DriverRef] = None


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
            # Fall back to a non-signal driver (e.g. rebalance) only when there is no
            # Signal — the Signal is the real thesis and takes precedence.
            driver = None
            if signal is None:
                drv = c.execute(
                    "SELECT kind, detail FROM drivers "
                    "WHERE symbol=? AND side=? AND substr(ts,1,10)=? "
                    "ORDER BY id DESC LIMIT 1", (symbol, side, today)).fetchone()
                driver = DriverRef(drv[0], drv[1]) if drv else None
            activity.append(TradeLine(side=side, symbol=symbol, qty=qty,
                                      avg_price=avg_price, signal=signal, driver=driver))
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

    @staticmethod
    def _pnl_pct(d: ReportData) -> float:
        base = (d.total_assets or 0.0) - (d.day_pnl or 0.0)
        return (d.day_pnl / base * 100) if (base and d.day_pnl is not None) else 0.0

    @staticmethod
    def _gross_pct(d: ReportData) -> float:
        return (d.gross_exposure / d.total_assets * 100) if (d.total_assets and d.gross_exposure is not None) else 0.0

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
                elif t.driver:
                    s += f"  [{t.driver.kind} {t.driver.detail}]"
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
                elif t.driver:
                    row += f"\n_{t.driver.kind} · {t.driver.detail}_"
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

    def send_eod_report(self, now: datetime) -> None:
        """Build and post the EOD summary. Never raises into the runner."""
        try:
            payload = self._render(self._gather(now))
        except Exception as e:
            logger.error("EOD report build failed: %s", e)
            return
        try:
            self._post_with_retry(payload)
        except Exception as e:  # defense in depth — the loop must never crash on a report
            logger.error("EOD report POST unexpected error: %s", e)

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
