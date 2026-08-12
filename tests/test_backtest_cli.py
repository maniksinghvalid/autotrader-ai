"""CLI smoke tests: everything wired to an injected fake client, no network,
no reliance on a real MASSIVE_API_KEY."""
from __future__ import annotations

import csv
from datetime import datetime, timezone

import pytest

from autotrader.backtest.__main__ import build_parser, main
from autotrader.backtest.data import Bar


def _ts(y, m, d, hh=14, mm=30):
    return int(datetime(y, m, d, hh, mm, tzinfo=timezone.utc).timestamp() * 1000)


def _bar(y, m, d, o, h, l, c):
    return Bar(ts=_ts(y, m, d), open=o, high=h, low=l, close=c, volume=1000)


def _sample_bars():
    bars = [_bar(2026, 1, 25 + i, 99, 100, 98, 99.5) for i in range(3)]
    bars += [
        _bar(2026, 2, 1, 99.5, 102, 99, 101.5),    # enters
        _bar(2026, 2, 2, 101, 130, 100, 128),      # takes profit
    ]
    return bars


class _FakeClient:
    def __init__(self, data):
        self._data = data

    def aggs(self, ticker, multiplier, timespan, frm, to, adjusted=True):
        return self._data.get(ticker, [])


def test_offline_smoke_writes_report_and_csvs(tmp_path, capsys):
    out_dir = tmp_path / "run1"
    cache_dir = tmp_path / "cache"
    argv = ["--symbols", "AAPL", "--from", "2026-02-01", "--to", "2026-03-01",
           "--lookback", "3", "--out-dir", str(out_dir), "--cache-dir", str(cache_dir)]
    fake = _FakeClient({"AAPL": _sample_bars()})
    rc = main(argv, client_factory=lambda: fake)
    assert rc == 0
    out = capsys.readouterr().out
    assert "Total return" in out

    assert (out_dir / "report.html").is_file()
    assert "AAPL" in (out_dir / "report.html").read_text(encoding="utf-8")
    assert (out_dir / "summary.json").is_file()

    with open(out_dir / "trades.csv", newline="") as fh:
        rows = list(csv.reader(fh))
    assert rows[0] == ["ts", "date", "symbol", "side", "qty", "price", "commission", "reason"]
    assert len(rows) == 3   # header + BUY + SELL

    with open(out_dir / "equity_curve.csv", newline="") as fh:
        erows = list(csv.reader(fh))
    assert erows[0] == ["date", "total_value", "cash"]
    assert len(erows) > 1


def test_no_save_writes_nothing(tmp_path, capsys):
    cache_dir = tmp_path / "cache"
    argv = ["--symbols", "AAPL", "--from", "2026-02-01", "--to", "2026-03-01",
           "--lookback", "3", "--no-save", "--cache-dir", str(cache_dir)]
    fake = _FakeClient({"AAPL": _sample_bars()})
    rc = main(argv, client_factory=lambda: fake)
    assert rc == 0
    assert list(tmp_path.glob("run*")) == []


def test_interval_parsing_valid_and_invalid():
    p = build_parser()
    args = p.parse_args(["--symbols", "AAPL", "--from", "2026-02-01", "--to", "2026-03-01",
                         "--interval", "5m"])
    assert args.interval == (5, "minute")
    args = p.parse_args(["--symbols", "AAPL", "--from", "2026-02-01", "--to", "2026-03-01",
                         "--interval", "1d"])
    assert args.interval == (1, "day")
    with pytest.raises(SystemExit) as exc:
        p.parse_args(["--symbols", "AAPL", "--from", "2026-02-01", "--to", "2026-03-01",
                     "--interval", "2x"])
    assert exc.value.code == 2


def test_stop_loss_pct_zero_is_clean_argparse_error(capsys):
    p = build_parser()
    with pytest.raises(SystemExit) as exc:
        p.parse_args(["--symbols", "AAPL", "--from", "2026-02-01", "--to", "2026-03-01",
                     "--stop-loss-pct", "0"])
    assert exc.value.code == 2
    assert "Traceback" not in capsys.readouterr().err


def test_symbol_with_zero_bars_is_skipped_others_proceed(tmp_path, capsys):
    fake = _FakeClient({"AAPL": _sample_bars(), "ZZZZ": []})
    argv = ["--symbols", "AAPL,ZZZZ", "--from", "2026-02-01", "--to", "2026-03-01",
           "--lookback", "3", "--no-save", "--cache-dir", str(tmp_path / "cache")]
    rc = main(argv, client_factory=lambda: fake)
    assert rc == 0
    err = capsys.readouterr().err
    assert "ZZZZ" in err and "skipping" in err


def test_all_symbols_empty_returns_exit_1(tmp_path, capsys):
    fake = _FakeClient({})
    argv = ["--symbols", "ZZZZ", "--from", "2026-02-01", "--to", "2026-03-01",
           "--no-save", "--cache-dir", str(tmp_path / "cache")]
    rc = main(argv, client_factory=lambda: fake)
    assert rc == 1
    err = capsys.readouterr().err
    assert "no data" in err


def test_strategy_variant_flags_flow_into_config(tmp_path, monkeypatch):
    """--strategy/--pullback-lookback/--regime-sma reach BacktestConfig, and
    the env fallbacks (STRATEGY_KIND etc.) are honored when flags are absent."""
    from autotrader.backtest.__main__ import build_parser

    args = build_parser().parse_args(
        ["--symbols", "AAPL", "--from", "2026-02-01", "--to", "2026-03-01",
         "--strategy", "pullback", "--pullback-lookback", "15", "--regime-sma", "100"])
    assert args.strategy == "pullback"
    assert args.pullback_lookback == 15
    assert args.regime_sma == 100

    monkeypatch.setenv("STRATEGY_KIND", "breakout_regime")
    monkeypatch.setenv("REGIME_SMA", "100")
    args = build_parser().parse_args(
        ["--symbols", "AAPL", "--from", "2026-02-01", "--to", "2026-03-01"])
    assert args.strategy == "breakout_regime"
    assert args.regime_sma == 100


def test_pullback_run_end_to_end_offline(tmp_path):
    """A pullback CLI run trades on a dip-in-uptrend series and writes outputs."""
    bars = [_bar(2026, 1, 20 + i, 99 + i * 0.1, 100 + i * 0.1, 98 + i * 0.1, 99.5 + i * 0.1)
            for i in range(6)]   # warmup uptrend
    bars += [
        _bar(2026, 2, 2, 100.4, 100.6, 97.5, 100.0),   # dips to the M-day low zone
        _bar(2026, 2, 3, 100.0, 103.0, 99.8, 102.5),   # recovers -> TP
    ]
    fake = _FakeClient({"AAPL": bars})
    out_dir = tmp_path / "pb"
    argv = ["--symbols", "AAPL", "--from", "2026-02-01", "--to", "2026-03-01",
            "--strategy", "pullback", "--pullback-lookback", "3", "--regime-sma", "3",
            "--stop-loss-pct", "0.05", "--take-profit-pct", "0.02",
            "--out-dir", str(out_dir), "--cache-dir", str(tmp_path / "cache")]
    rc = main(argv, client_factory=lambda: fake)
    assert rc == 0
    assert (out_dir / "report.html").is_file()
