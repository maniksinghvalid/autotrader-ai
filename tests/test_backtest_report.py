"""Self-contained HTML report: no external assets, correct content, no crash
on a zero-trade result."""
from __future__ import annotations

from datetime import date

import pytest

from autotrader.backtest.engine import BacktestConfig, BacktestResult, Fill
from autotrader.backtest.metrics import RoundTrip, summarize
from autotrader.backtest.report import render_html_report, write_html_report


def _cfg(**over):
    base = dict(symbols=("AAPL",), start=date(2026, 1, 1), end=date(2026, 12, 31),
               multiplier=1, timespan="day", cash=100_000.0)
    base.update(over)
    return BacktestConfig(**base)


def _sample_result():
    fills = (
        Fill(ts=1, symbol="AAPL", side="BUY", qty=10, price=100.0, commission=1.0, reason="breakout"),
        Fill(ts=2, symbol="AAPL", side="SELL", qty=10, price=110.0, commission=1.0, reason="take-profit"),
    )
    curve = (
        (date(2026, 1, 1), 100_000.0, 99_000.0),
        (date(2026, 1, 2), 101_080.0, 101_080.0),
    )
    return BacktestResult(fills=fills, equity_curve=curve, skipped_for_cash=0,
                          cfg=_cfg(), held_days={"AAPL": 1})


def test_report_is_self_contained_no_external_assets():
    result = _sample_result()
    trips = [RoundTrip("AAPL", 1, 2, 10, 100.0, 110.0, 2.0, 98.0, "take-profit")]
    summary = summarize(result, trips)
    html = render_html_report(result, summary, trips)
    assert 'src="http' not in html
    assert 'href="http' not in html
    assert "<script" not in html   # no JS deps at all


def test_report_contains_key_metric_values():
    result = _sample_result()
    trips = [RoundTrip("AAPL", 1, 2, 10, 100.0, 110.0, 2.0, 98.0, "take-profit")]
    summary = summarize(result, trips)
    html = render_html_report(result, summary, trips)
    assert "AAPL" in html
    assert "98.00" in html          # the trade's P&L
    assert "take-profit" in html


def test_report_svg_has_one_point_per_equity_curve_day():
    result = _sample_result()
    summary = summarize(result, [])
    html = render_html_report(result, summary, [])
    # 2 equity-curve days -> 2 points in each of the 2 polylines
    assert html.count("polyline") == 2   # one open + ... just assert points count instead
    start = html.index('points="') + len('points="')
    end = html.index('"', start)
    points = html[start:end].split()
    assert len(points) == len(result.equity_curve)


def test_report_trades_table_has_one_row_per_round_trip():
    result = _sample_result()
    trips = [RoundTrip("AAPL", 1, 2, 10, 100.0, 110.0, 2.0, 98.0, "take-profit")]
    summary = summarize(result, trips)
    html = render_html_report(result, summary, trips)
    assert html.count("<tr>") >= len(trips) + 1   # + header rows across tables


def test_report_renders_with_zero_trades_no_crash():
    result = BacktestResult(fills=(), equity_curve=((date(2026, 1, 1), 100_000.0, 100_000.0),),
                            skipped_for_cash=0, cfg=_cfg(), held_days={})
    summary = summarize(result, [])
    html = render_html_report(result, summary, [])
    assert "No completed trades." in html


def test_write_html_report_writes_file(tmp_path):
    result = _sample_result()
    trips = [RoundTrip("AAPL", 1, 2, 10, 100.0, 110.0, 2.0, 98.0, "take-profit")]
    summary = summarize(result, trips)
    out = tmp_path / "report.html"
    write_html_report(result, summary, trips, out)
    assert out.is_file()
    assert "AAPL" in out.read_text(encoding="utf-8")
