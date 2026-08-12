"""Self-contained HTML report — the backtest's "UI". A backtest is a batch
job; its UI is a report artifact, not a server (see docs/BACKTESTING.md).
Renders inline CSS + SVG only — no external assets, no JS — so report.html
opens standalone in any browser, offline. Stdlib only (html.escape)."""
from __future__ import annotations

from datetime import datetime, timezone
from html import escape
from pathlib import Path
from typing import List
from zoneinfo import ZoneInfo

from autotrader.backtest.engine import BacktestConfig, BacktestResult
from autotrader.backtest.metrics import RoundTrip

_ET = ZoneInfo("America/New_York")

_CSS = """
body{font-family:-apple-system,Segoe UI,Helvetica,Arial,sans-serif;background:#0b0d12;color:#e5e7eb;margin:0;padding:24px}
h1{font-size:1.3rem;margin:0 0 4px} h2{font-size:1.05rem;margin:28px 0 10px;color:#cbd5e1}
.meta{color:#9ca3af;font-size:0.85rem;margin-bottom:20px}
.cards{display:flex;flex-wrap:wrap;gap:10px}
.card{background:#151922;border:1px solid #262b36;border-radius:8px;padding:10px 14px;min-width:120px}
.card .label{font-size:0.72rem;color:#9ca3af;text-transform:uppercase;letter-spacing:.03em}
.card .value{font-size:1.15rem;margin-top:2px}
.pos{color:#4ade80} .neg{color:#f87171}
table{border-collapse:collapse;width:100%;font-size:0.85rem;margin-top:8px}
th,td{text-align:right;padding:5px 10px;border-bottom:1px solid #1f2430}
th{color:#9ca3af;font-weight:600} th:first-child,td:first-child{text-align:left}
svg{background:#10131a;border:1px solid #262b36;border-radius:8px}
.legend{font-size:0.78rem;color:#9ca3af;margin-top:4px}
.legend .sw{display:inline-block;width:10px;height:10px;border-radius:2px;margin-right:4px;vertical-align:middle}
"""


def _fmt_pct(x) -> str:
    if x is None:
        return "—"
    return f"{x * 100:.2f}%"


def _fmt_money(x) -> str:
    return f"${x:,.2f}"


def _fmt_num(x) -> str:
    return f"{x:.2f}"


def _cls(x) -> str:
    return "pos" if x > 0 else ("neg" if x < 0 else "")


def _ts_to_iso_date(ts: int) -> str:
    return datetime.fromtimestamp(ts / 1000, tz=timezone.utc).astimezone(_ET).date().isoformat()


def _svg_equity_curve(curve, width: int = 760, height: int = 220) -> str:
    """Two polylines (total value, cash). # ponytail: no drawdown shading —
    the two lines already show exposure; add shading if it's ever asked for."""
    if len(curve) < 2:
        return f'<svg viewBox="0 0 {width} {height}"></svg>'
    values = [v for _, v, _ in curve]
    cashes = [c for _, _, c in curve]
    vmin, vmax = min(values + cashes), max(values + cashes)
    if vmax == vmin:
        vmax = vmin + 1.0
    n = len(curve)
    pad_l, pad_r, pad_t, pad_b = 45, 15, 15, 25

    def x(i):
        return pad_l + (width - pad_l - pad_r) * (i / (n - 1))

    def y(v):
        return height - pad_b - (height - pad_t - pad_b) * (v - vmin) / (vmax - vmin)

    value_pts = " ".join(f"{x(i):.1f},{y(v):.1f}" for i, (_, v, _) in enumerate(curve))
    cash_pts = " ".join(f"{x(i):.1f},{y(c):.1f}" for i, (_, _, c) in enumerate(curve))
    first_day, last_day = curve[0][0].isoformat(), curve[-1][0].isoformat()
    return (
        f'<svg viewBox="0 0 {width} {height}" xmlns="http://www.w3.org/2000/svg" '
        f'role="img" aria-label="Equity curve">'
        f'<polyline points="{value_pts}" fill="none" stroke="#3b82f6" stroke-width="2"/>'
        f'<polyline points="{cash_pts}" fill="none" stroke="#9ca3af" stroke-width="1.5" '
        f'stroke-dasharray="4,3"/>'
        f'<text x="{pad_l}" y="{height - 6}" fill="#6b7280" font-size="10">{escape(first_day)}</text>'
        f'<text x="{width - pad_r}" y="{height - 6}" fill="#6b7280" font-size="10" '
        f'text-anchor="end">{escape(last_day)}</text>'
        f'</svg>'
    )


def _metric_cards(p: dict) -> str:
    rows = [
        ("Total return", _fmt_pct(p["total_return"]), _cls(p["total_return"])),
        ("CAGR", _fmt_pct(p["cagr"]), _cls(p["cagr"])),
        ("Sharpe", _fmt_num(p["sharpe"]), _cls(p["sharpe"])),
        ("Max drawdown", _fmt_pct(-p["max_drawdown"]), "neg" if p["max_drawdown"] else ""),
        ("Win rate", _fmt_pct(p["win_rate"]), ""),
        ("Profit factor", _fmt_num(p["profit_factor"]) if p["profit_factor"] is not None else "—", ""),
        ("Exposure", _fmt_pct(p["exposure"]), ""),
        ("Final value", _fmt_money(p["final_value"]), ""),
        ("Trades", str(p["trade_count"]), ""),
        ("Skipped (cash)", str(p["skipped_for_cash"]), ""),
    ]
    cards = "".join(
        f'<div class="card"><div class="label">{escape(label)}</div>'
        f'<div class="value {cls}">{value}</div></div>'
        for label, value, cls in rows
    )
    return f'<div class="cards">{cards}</div>'


def _per_symbol_table(per_symbol: dict) -> str:
    header = ("<tr><th>Symbol</th><th>Trades</th><th>Win rate</th><th>Avg win</th>"
             "<th>Avg loss</th><th>Profit factor</th><th>Exposure</th><th>Realized P&amp;L</th></tr>")
    rows = []
    for sym, s in sorted(per_symbol.items()):
        pf = _fmt_num(s["profit_factor"]) if s["profit_factor"] is not None else "—"
        rows.append(
            f'<tr><td>{escape(sym)}</td><td>{s["trade_count"]}</td>'
            f'<td>{_fmt_pct(s["win_rate"])}</td><td>{_fmt_money(s["avg_win"])}</td>'
            f'<td>{_fmt_money(s["avg_loss"])}</td><td>{pf}</td>'
            f'<td>{_fmt_pct(s["exposure"])}</td>'
            f'<td class="{_cls(s["realized_pnl"])}">{_fmt_money(s["realized_pnl"])}</td></tr>')
    return f'<table>{header}{"".join(rows)}</table>'


def _trades_table(trips: List[RoundTrip]) -> str:
    if not trips:
        return "<p>No completed trades.</p>"
    header = ("<tr><th>Symbol</th><th>Entry date</th><th>Entry px</th><th>Exit date</th>"
             "<th>Exit px</th><th>Qty</th><th>Reason</th><th>P&amp;L</th><th>Cumulative</th></tr>")
    rows = []
    cum = 0.0
    for t in sorted(trips, key=lambda x: x.exit_ts):
        cum += t.pnl
        rows.append(
            f'<tr><td>{escape(t.symbol)}</td><td>{_ts_to_iso_date(t.entry_ts)}</td>'
            f'<td>{_fmt_money(t.entry_price)}</td><td>{_ts_to_iso_date(t.exit_ts)}</td>'
            f'<td>{_fmt_money(t.exit_price)}</td><td>{t.qty}</td>'
            f'<td>{escape(t.exit_reason)}</td>'
            f'<td class="{_cls(t.pnl)}">{_fmt_money(t.pnl)}</td>'
            f'<td class="{_cls(cum)}">{_fmt_money(cum)}</td></tr>')
    return f'<table>{header}{"".join(rows)}</table>'


def render_html_report(result: BacktestResult, summary: dict, trips: List[RoundTrip]) -> str:
    cfg: BacktestConfig = result.cfg
    meta = (
        f"Symbols: {escape(', '.join(cfg.symbols))} &middot; "
        f"{cfg.start.isoformat()} → {cfg.end.isoformat()} &middot; "
        f"Interval: {cfg.multiplier}{cfg.timespan} &middot; "
        f"Starting capital: {_fmt_money(cfg.cash)} &middot; "
        f"Commission: {_fmt_money(cfg.commission_per_order)}/order"
        f"+{_fmt_money(cfg.commission_per_share)}/share &middot; "
        f"Slippage: {cfg.slippage_bps:.1f}bps &middot; "
        f"Lookback: {cfg.lookback}d &middot; "
        f"SL/TP: {_fmt_pct(cfg.stop_loss_pct)}/{_fmt_pct(cfg.take_profit_pct)} &middot; "
        f"Trailing: {cfg.trailing_stop_pct:.1f}%"
    )
    return f"""<!doctype html><html><head><meta charset="utf-8">
<title>AutoTrader Backtest Report</title><style>{_CSS}</style></head><body>
<h1>AutoTrader Backtest Report</h1>
<div class="meta">{meta}</div>
{_metric_cards(summary["portfolio"])}
<h2>Equity curve</h2>
{_svg_equity_curve(result.equity_curve)}
<div class="legend"><span class="sw" style="background:#3b82f6"></span>Total value
&nbsp;&nbsp;<span class="sw" style="background:#9ca3af"></span>Cash</div>
<h2>Per-symbol</h2>
{_per_symbol_table(summary["per_symbol"])}
<h2>Trades</h2>
{_trades_table(trips)}
</body></html>"""


def write_html_report(result: BacktestResult, summary: dict, trips: List[RoundTrip], path) -> None:
    Path(path).write_text(render_html_report(result, summary, trips), encoding="utf-8")
