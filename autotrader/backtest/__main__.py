"""CLI: `python -m autotrader.backtest`.

Fetches historical bars from Massive, runs the production breakout strategy
against them, and writes a report. Strategy knobs default from the SAME env
vars autotrader.main reads (ENTRY_BREAKOUT_LOOKBACK, STRATEGY_*,
RISK_TRAILING_STOP_PCT, ORDER_QTY) so a sourced config/risk.config gives
config parity with production."""
from __future__ import annotations

import argparse
import csv
import json
import math
import os
import re
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from autotrader.backtest.data import DEFAULT_CACHE_DIR, MassiveClient, MassiveError, load_bars, normalize_ticker
from autotrader.backtest.engine import BacktestConfig, run_backtest
from autotrader.backtest.metrics import pair_round_trips, summarize
from autotrader.backtest.report import write_html_report
from autotrader.config import load_risk_config

_ET = ZoneInfo("America/New_York")
_INTERVAL_RE = re.compile(r"^(\d+)([mhd])$")
_TIMESPAN = {"m": "minute", "h": "hour", "d": "day"}
DEFAULT_RUNS_DIR = Path.home() / ".autotrader" / "backtest_runs"


def _parse_interval(raw: str):
    m = _INTERVAL_RE.match(raw.strip().lower())
    if not m:
        raise argparse.ArgumentTypeError(
            f"--interval must look like '1d', '5m', or '1h' (got {raw!r})")
    return int(m.group(1)), _TIMESPAN[m.group(2)]


def _parse_date(raw: str) -> date:
    try:
        return date.fromisoformat(raw)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"invalid date {raw!r}, expected YYYY-MM-DD") from exc


def _positive_pct(raw: str) -> float:
    v = float(raw)
    if v <= 0:
        raise argparse.ArgumentTypeError("must be > 0 — an explicit stop/target is required")
    return v


def _ts_to_date(ts: int) -> str:
    return datetime.fromtimestamp(ts / 1000, tz=timezone.utc).astimezone(_ET).date().isoformat()


def _utc_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m autotrader.backtest",
        description="Backtest the production breakout strategy against historical Massive API data.")
    p.add_argument("--symbols", required=True,
                   help="Comma-separated tickers, e.g. AAPL,MSFT or US.AAPL,US.MSFT")
    p.add_argument("--from", dest="start", required=True, type=_parse_date)
    p.add_argument("--to", dest="end", required=True, type=_parse_date)
    p.add_argument("--interval", default=(1, "day"), type=_parse_interval)
    p.add_argument("--cash", type=float, default=100_000.0)
    p.add_argument("--commission", type=float, default=0.0)
    p.add_argument("--commission-per-share", type=float, default=0.0)
    p.add_argument("--slippage-bps", type=float, default=0.0)
    p.add_argument("--lookback", type=int, default=int(os.getenv("ENTRY_BREAKOUT_LOOKBACK", "20")))
    p.add_argument("--stop-loss-pct", type=_positive_pct,
                   default=float(os.getenv("STRATEGY_STOP_LOSS_PCT", "0.05")))
    p.add_argument("--take-profit-pct", type=_positive_pct,
                   default=float(os.getenv("STRATEGY_TAKE_PROFIT_PCT", "0.10")))
    p.add_argument("--trailing-stop-pct", type=float,
                   default=float(os.getenv("RISK_TRAILING_STOP_PCT", "0.0")))
    p.add_argument("--confidence", type=float, default=float(os.getenv("STRATEGY_CONFIDENCE", "0.7")))
    p.add_argument("--order-qty", type=int, default=int(os.getenv("ORDER_QTY", "1")))
    p.add_argument("--strategy", choices=["breakout", "breakout_regime", "pullback"],
                   default=os.getenv("STRATEGY_KIND", "breakout"),
                   help="Strategy variant (env STRATEGY_KIND); default breakout")
    p.add_argument("--regime-sma", type=int, default=int(os.getenv("REGIME_SMA", "200")),
                   help="Uptrend-gate SMA window for breakout_regime/pullback (env REGIME_SMA)")
    p.add_argument("--pullback-lookback", type=int,
                   default=int(os.getenv("ENTRY_PULLBACK_LOOKBACK", "10")),
                   help="Pullback M-day-low window (env ENTRY_PULLBACK_LOOKBACK)")
    p.add_argument("--pullback-depth-pct", type=float, default=0.0,
                   help="0 = trigger at min of last M lows; >0 = SMA*(1-depth)")
    p.add_argument("--out-dir", default=None,
                   help="Default: ~/.autotrader/backtest_runs/<UTC-timestamp>_<symbols>/")
    p.add_argument("--no-save", action="store_true", help="Print summary only; write nothing")
    p.add_argument("--cache-dir", default=str(DEFAULT_CACHE_DIR))
    p.add_argument("--no-cache", action="store_true")
    p.add_argument("--api-key", default=os.getenv("MASSIVE_API_KEY", ""))
    return p


def _print_summary(cfg: BacktestConfig, summary: dict) -> None:
    p = summary["portfolio"]
    print(f"Symbols: {', '.join(cfg.symbols)}   {cfg.start} -> {cfg.end}   "
         f"interval={cfg.multiplier}{cfg.timespan}")
    print(f"Total return: {p['total_return'] * 100:.2f}%   CAGR: {p['cagr'] * 100:.2f}%   "
         f"Sharpe: {p['sharpe']:.2f}   Max DD: {p['max_drawdown'] * 100:.2f}%")
    pf = f"{p['profit_factor']:.2f}" if p["profit_factor"] is not None else "n/a"
    print(f"Trades: {p['trade_count']}   Win rate: {p['win_rate'] * 100:.1f}%   Profit factor: {pf}")
    print(f"Final value: ${p['final_value']:,.2f}   Exposure: {p['exposure'] * 100:.1f}%   "
         f"Skipped (cash): {p['skipped_for_cash']}")
    if summary["per_symbol"]:
        print()
        print(f"{'Symbol':<8}{'Trades':>8}{'WinRate':>10}{'AvgWin':>12}{'AvgLoss':>12}"
             f"{'PF':>8}{'Exposure':>10}{'RealizedPnL':>14}")
        for sym, s in sorted(summary["per_symbol"].items()):
            spf = f"{s['profit_factor']:.2f}" if s["profit_factor"] is not None else "n/a"
            print(f"{sym:<8}{s['trade_count']:>8}{s['win_rate'] * 100:>9.1f}%{s['avg_win']:>12.2f}"
                 f"{s['avg_loss']:>12.2f}{spf:>8}{s['exposure'] * 100:>9.1f}%{s['realized_pnl']:>14.2f}")


def _write_equity_csv(result, path: Path) -> None:
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["date", "total_value", "cash"])
        for day, value, cash in result.equity_curve:
            w.writerow([day.isoformat(), f"{value:.2f}", f"{cash:.2f}"])


def _write_trades_csv(result, path: Path) -> None:
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["ts", "date", "symbol", "side", "qty", "price", "commission", "reason"])
        for f in sorted(result.fills, key=lambda x: x.ts):
            w.writerow([f.ts, _ts_to_date(f.ts), f.symbol, f.side, f.qty,
                       f"{f.price:.4f}", f"{f.commission:.4f}", f.reason])


def main(argv=None, client_factory=None) -> int:
    args = build_parser().parse_args(argv)

    if args.start > args.end:
        build_parser().error("--from must not be after --to")

    symbols = tuple(sorted({normalize_ticker(s) for s in args.symbols.split(",") if s.strip()}))
    if not symbols:
        build_parser().error("--symbols must contain at least one ticker")

    multiplier, timespan = args.interval
    # Warmup must cover the LARGEST indicator any selected variant needs —
    # an SMA(200) with insufficient history silently never trades (fail-safe),
    # which would corrupt the run without erroring.
    max_window = args.lookback
    if args.strategy in ("breakout_regime", "pullback"):
        max_window = max(max_window, args.regime_sma)
    if args.strategy == "pullback":
        max_window = max(max_window, args.pullback_lookback)
    warmup_days = math.ceil(max_window * 1.6 + 10)
    fetch_start = args.start - timedelta(days=warmup_days)
    cache_dir = Path(args.cache_dir)
    use_cache = not args.no_cache
    factory = client_factory or (lambda: MassiveClient(args.api_key))

    bars_by_symbol = {}
    for sym in symbols:
        try:
            bars = load_bars(factory, cache_dir, sym, multiplier, timespan,
                             fetch_start.isoformat(), args.end.isoformat(), use_cache=use_cache)
        except MassiveError as exc:
            print(f"warning: {sym}: {exc}", file=sys.stderr)
            bars = []
        if not bars:
            print(f"warning: {sym}: no data returned — skipping", file=sys.stderr)
            continue
        bars_by_symbol[sym] = bars

    if not bars_by_symbol:
        print("error: no data for any requested symbol", file=sys.stderr)
        return 1

    cfg = BacktestConfig(
        symbols=tuple(sorted(bars_by_symbol)), start=args.start, end=args.end,
        multiplier=multiplier, timespan=timespan, cash=args.cash,
        commission_per_order=args.commission, commission_per_share=args.commission_per_share,
        slippage_bps=args.slippage_bps, lookback=args.lookback,
        stop_loss_pct=args.stop_loss_pct, take_profit_pct=args.take_profit_pct,
        trailing_stop_pct=args.trailing_stop_pct, confidence=args.confidence,
        order_qty=args.order_qty,
        strategy=args.strategy, regime_sma=args.regime_sma,
        pullback_lookback=args.pullback_lookback,
        pullback_depth_pct=args.pullback_depth_pct,
    )
    risk_cfg = load_risk_config()

    result = run_backtest(bars_by_symbol, cfg, risk_cfg)
    trips = pair_round_trips(list(result.fills))
    summary = summarize(result, trips)

    _print_summary(cfg, summary)

    if not args.no_save:
        out_dir = Path(args.out_dir) if args.out_dir else (
            DEFAULT_RUNS_DIR / f"{_utc_stamp()}_{'-'.join(cfg.symbols)}")
        out_dir.mkdir(parents=True, exist_ok=True)
        write_html_report(result, summary, trips, out_dir / "report.html")
        _write_equity_csv(result, out_dir / "equity_curve.csv")
        _write_trades_csv(result, out_dir / "trades.csv")
        (out_dir / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
        print(f"\nWrote report to {out_dir}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
