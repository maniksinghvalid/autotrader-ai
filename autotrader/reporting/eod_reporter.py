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
