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

from autotrader.reporting.option_code import parse_option_code
from autotrader.reporting.classify import Leg, classify_strategy
from autotrader.reporting.economics import StrategyEconomics, compute_economics
from autotrader.reporting.capital_flow import CapitalFlow, compute_capital_flow

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
class StrategyGroup:
    underlying: str
    label: str
    legs: Tuple[Leg, ...]
    economics: StrategyEconomics
    basis: Optional[float]
    signal: Optional[SignalRef]
    driver: Optional[DriverRef]


@dataclass(frozen=True)
class ReportData:
    date_label: str
    trading_env: str
    realized_pnl: Optional[float]
    unrealized_pnl: Optional[float]
    total_assets: Optional[float]
    cash: Optional[float]
    gross_exposure: Optional[float]
    capital_flow: Optional[CapitalFlow]
    groups: Tuple[StrategyGroup, ...]
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

    _OVERLAY_PREFIXES = ("COVERED_CALL", "PROTECTIVE_PUT", "COLLAR",
                         "BEAR_PUT_SPREAD", "CALL_DIAGONAL", "LEAP")

    @classmethod
    def _strip_overlay_prefix(cls, rationale: str) -> str:
        # Signal rationale for an overlay is stored as "COVERED_CALL: <thesis>".
        head, sep, tail = rationale.partition(": ")
        return tail if (sep and head in cls._OVERLAY_PREFIXES) else rationale

    @staticmethod
    def _underlying_of(symbol: str) -> str:
        parsed = parse_option_code(symbol)
        return parsed.underlying if parsed is not None else symbol

    def _gather(self, now: datetime) -> ReportData:
        today = now.date().isoformat()
        c = self._db._conn
        perf = c.execute(
            "SELECT day_pnl, total_assets, cash, gross_exposure, unrealized_pnl "
            "FROM performance WHERE date=?", (today,)).fetchone()
        fill_rows = c.execute(
            "SELECT symbol, side, SUM(qty) AS q, SUM(qty*price)/SUM(qty) AS avg_price "
            "FROM fills WHERE substr(ts,1,10)=? GROUP BY symbol, side "
            "ORDER BY symbol, side", (today,)).fetchall()

        # Group fills by underlying; each (symbol, side) aggregate becomes one Leg.
        by_under: dict = {}
        for symbol, side, qty, avg_price in fill_rows:
            under = self._underlying_of(symbol)
            parsed = parse_option_code(symbol)
            by_under.setdefault(under, []).append(
                Leg(side=side, qty=qty, price=avg_price, option=parsed))

        groups = []
        for under, legs in by_under.items():
            label = classify_strategy(legs)
            # basis: today's stock-leg avg fill, else position avg_price, else None.
            stock_leg = next((l for l in legs if l.option is None), None)
            if stock_leg is not None:
                basis = stock_leg.price
            else:
                prow = c.execute(
                    "SELECT avg_price FROM positions WHERE symbol=?", (under,)).fetchone()
                basis = prow[0] if prow else None
            econ = compute_economics(legs, basis)
            sig = c.execute(
                "SELECT direction, confidence, rationale FROM signals "
                "WHERE symbol=? AND substr(ts,1,10)=? ORDER BY id DESC LIMIT 1",
                (under, today)).fetchone()
            signal = (SignalRef(sig[0], sig[1], self._strip_overlay_prefix(sig[2]))
                      if sig else None)
            driver = None
            if signal is None:
                drv = c.execute(
                    "SELECT kind, detail FROM drivers "
                    "WHERE symbol=? AND substr(ts,1,10)=? ORDER BY id DESC LIMIT 1",
                    (under, today)).fetchone()
                driver = DriverRef(drv[0], drv[1]) if drv else None
            groups.append(StrategyGroup(
                underlying=under, label=label, legs=tuple(legs), economics=econ,
                basis=basis, signal=signal, driver=driver))

        # Order: largest absolute capital deployed first; stock-only exits last.
        def _deployed(g: StrategyGroup) -> float:
            return sum(abs(l.qty * l.price * (l.option.multiplier if l.option else 1))
                       for l in g.legs)
        groups.sort(key=lambda g: (g.label in ("Stock exit",), -_deployed(g)))

        capital_flow = (compute_capital_flow(
            (r[0], r[1], r[2], r[3]) for r in fill_rows) if fill_rows else None)
        positions = c.execute(
            "SELECT symbol, qty FROM positions WHERE qty != 0 ORDER BY symbol").fetchall()
        return ReportData(
            date_label=now.strftime("%a, %b %d %Y"),
            trading_env=self._env,
            realized_pnl=perf[0] if perf else None,
            unrealized_pnl=perf[4] if perf else None,
            total_assets=perf[1] if perf else None,
            cash=perf[2] if perf else None,
            gross_exposure=perf[3] if perf else None,
            capital_flow=capital_flow,
            groups=tuple(groups),
            positions=tuple((r[0], r[1]) for r in positions),
        )

    @staticmethod
    def _pnl_pct(d: ReportData) -> float:
        base = (d.total_assets or 0.0) - (d.realized_pnl or 0.0)
        return (d.realized_pnl / base * 100) if (base and d.realized_pnl is not None) else 0.0

    _GROSS_UNAVAILABLE = "exposure unavailable — snapshot stale"

    @staticmethod
    def _gross_text(d: ReportData) -> str:
        if d.gross_exposure is None or d.total_assets is None:
            return EODReporter._GROSS_UNAVAILABLE
        pct = (d.gross_exposure / d.total_assets * 100) if d.total_assets else 0.0
        return f"{pct:.0f}%"

    @staticmethod
    def _realized_text(d: ReportData) -> str:
        return "—" if d.realized_pnl is None else f"{d.realized_pnl:+,.2f}"

    @staticmethod
    def _unrealized_text(d: ReportData) -> str:
        if d.gross_exposure is None:          # snapshot stale -> not trustworthy
            return "unavailable"
        if d.unrealized_pnl is None:
            return "—"
        return f"{d.unrealized_pnl:+,.2f}"

    @staticmethod
    def _capital_flow_verb(net: float) -> str:
        # Value/formula unchanged; only the verb follows the sign.
        if net >= 0:
            return f"Net cash raised +{net:,.0f}"
        return f"Net cash deployed −{abs(net):,.0f}"

    _EMOJI = {"Covered Call": "🟢", "Covered Call (existing shares)": "🟢",
              "Protective Put": "🛡️", "Collar": "🔵", "Bear Put Spread": "🔻",
              "LEAP": "🚀", "PMCC / Call Diagonal": "🚀",
              "Stock entry": "🟢", "Stock exit": "⚪", "Strategy": "▫️"}

    @staticmethod
    def _short(sym: str) -> str:
        return sym.split(".", 1)[1] if "." in sym else sym

    def _leg_line(self, leg: Leg, asof) -> str:
        verb = "Bought" if leg.side == "BUY" else "Sold"
        if leg.option is None:
            return f"{verb} {int(leg.qty)} sh @ {leg.price:,.2f}"
        o = leg.option
        dte = (o.expiry - asof).days
        return (f"{verb} {int(leg.qty)}× ${o.strike:,.2f} {o.right.title()} "
                f"exp {o.expiry:%b %d} ({dte}d) @ {leg.price:,.2f}")

    @staticmethod
    def _econ_line(e: StrategyEconomics) -> Optional[str]:
        parts = []
        if e.net_premium is not None and e.net_premium != 0:
            kind = "credit" if e.net_premium > 0 else "debit"
            parts.append(f"Net {kind} {e.net_premium:+,.0f}")
        if e.cap is not None:
            parts.append(f"cap ${e.cap:,.2f}"
                         + (f" ({e.cap_pct:+.1f}%)" if e.cap_pct is not None else ""))
        if e.floor is not None:
            parts.append(f"floor ${e.floor:,.2f}"
                         + (f" ({e.floor_pct:+.1f}%)" if e.floor_pct is not None else ""))
        if e.hedge_cost_pct is not None:
            parts.append(f"hedge cost {e.hedge_cost_pct:.1f}%")
        return " · ".join(parts) if parts else None

    def _group_text(self, g: StrategyGroup, asof) -> str:
        emoji = self._EMOJI.get(g.label, "▫️")
        conf = f"  conf {g.signal.confidence:.2f}" if g.signal else ""
        head = f"{emoji} {g.label} — {self._short(g.underlying)}{conf}"
        body = [self._leg_line(l, asof) for l in g.legs]
        econ = self._econ_line(g.economics)
        if econ:
            body.append(econ)
        if g.signal and g.signal.rationale:
            body.append(f"Thesis: {g.signal.rationale}")
        elif g.driver:
            body.append(f"driver: {g.driver.kind} · {g.driver.detail}")
        return head + "\n" + "\n".join(f"   {b}" for b in body)

    def _header_lines(self, d: ReportData) -> list:
        lines = [f"Session summary — {d.date_label} ({d.trading_env})"]
        if d.total_assets is not None:
            pct = "" if d.realized_pnl is None else f" ({self._pnl_pct(d):+.2f}%)"
            lines.append(
                f"Realized {self._realized_text(d)}{pct} · "
                f"Unrealized {self._unrealized_text(d)} · "
                f"Assets {d.total_assets:,.0f} · Cash {d.cash:,.0f} · "
                f"Gross exp {self._gross_text(d)}")
        cf = d.capital_flow
        if cf is not None:
            lines.append(
                f"Premium collected {cf.premium_collected:+,.0f} · "
                f"Premium paid {-cf.premium_paid:+,.0f} · "
                f"{self._capital_flow_verb(cf.net_cash_deployed)}")
        return lines

    def _render(self, d: ReportData) -> dict:
        asof = datetime.strptime(d.date_label, "%a, %b %d %Y").date()
        lines = list(self._header_lines(d))
        if d.groups:
            leg_count = sum(len(g.legs) for g in d.groups)
            lines.append(f"Activity — {len(d.groups)} strategies, {leg_count} legs")
            for g in d.groups:
                lines.append(self._group_text(g, asof))
        else:
            lines.append("Activity — no trades today")
        pos = " · ".join(f"{sym} {qty}" for sym, qty in d.positions) or "none"
        lines.append(f"Open positions ({len(d.positions)}): {pos}")
        return {"text": "\n".join(lines), "blocks": self._build_blocks(d, asof)}

    def _build_blocks(self, d: ReportData, asof) -> list:
        blocks = [{"type": "header", "text": {"type": "plain_text",
                   "text": f"Session summary — {d.date_label} ({d.trading_env})"}}]
        if d.total_assets is not None:
            pct = "" if d.realized_pnl is None else f" ({self._pnl_pct(d):+.2f}%)"
            fields = [
                {"type": "mrkdwn", "text": f"*Realized*\n{self._realized_text(d)}{pct}"},
                {"type": "mrkdwn", "text": f"*Unrealized*\n{self._unrealized_text(d)}"},
                {"type": "mrkdwn", "text": f"*Total assets*\n{d.total_assets:,.0f}"},
                {"type": "mrkdwn", "text": f"*Cash*\n{d.cash:,.0f}"},
                {"type": "mrkdwn", "text": f"*Gross exp.*\n{self._gross_text(d)}"},
            ]
            blocks.append({"type": "section", "fields": fields})
        if d.capital_flow is not None:
            cf = d.capital_flow
            blocks.append({"type": "section", "text": {"type": "mrkdwn",
                "text": (f"*Capital flow*\nPremium collected {cf.premium_collected:+,.0f} · "
                         f"Premium paid {-cf.premium_paid:+,.0f} · "
                         f"{self._capital_flow_verb(cf.net_cash_deployed)}")}})
        blocks.append({"type": "divider"})
        if d.groups:
            for g in d.groups:
                blocks.append({"type": "section", "text": {"type": "mrkdwn",
                               "text": self._group_text(g, asof)}})
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
