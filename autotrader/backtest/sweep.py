"""Parameter/variant sweep with out-of-sample validation.

Run as `python -m autotrader.backtest.sweep`. Optimizes on a train window,
confirms the top-K candidates per variant family on an unseen validation
window, and ranks the final table by VALIDATION metrics — the targets only
"count" out-of-sample. Constraint ranking is lexicographic (hard max-drawdown
cap, then win rate, then Sharpe) rather than a composite score, so a failed
constraint is always visible instead of averaged away.

This is research tooling: results are historical simulation, not investment
advice, and no configuration is a guarantee of future performance."""
from __future__ import annotations

import argparse
import csv
import math
import os
import sys
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from autotrader.backtest.data import Bar, DEFAULT_CACHE_DIR, MassiveClient, MassiveError, load_bars, normalize_ticker
from autotrader.backtest.engine import BacktestConfig, run_backtest
from autotrader.backtest.metrics import pair_round_trips, summarize
from autotrader.backtest.report import write_html_report
from autotrader.backtest.__main__ import DEFAULT_RUNS_DIR, _write_equity_csv, _write_trades_csv, _print_summary
from autotrader.config import load_risk_config

DEFAULT_SYMBOLS = "CLOV,DIVO,IAU,IBIT,NIO,O,SCHF"

# Explicit bounded grids — 486 combos total (108 + 216 + 162), ~6ms per engine run.
_BREAKOUT_AXES = {
    "lookback": [10, 20, 40, 55],
    "stop_loss_pct": [0.02, 0.03, 0.05],
    "take_profit_pct": [0.03, 0.05, 0.10],
    "trailing_stop_pct": [0.0, 5.0, 8.0],
}
_PULLBACK_AXES = {
    "pullback_lookback": [5, 10, 15],
    "pullback_depth_pct": [0.0, 0.02, 0.05],
    "stop_loss_pct": [0.02, 0.03, 0.05],
    "take_profit_pct": [0.02, 0.03, 0.05],
    "regime_sma": [100, 200],
}

PARAM_COLUMNS = ["strategy", "lookback", "regime_sma", "pullback_lookback",
                 "pullback_depth_pct", "stop_loss_pct", "take_profit_pct",
                 "trailing_stop_pct"]
METRIC_COLUMNS = ["trades", "win_rate", "total_return", "max_drawdown",
                  "sharpe", "profit_factor", "avg_win", "avg_loss"]


def _product(axes: dict) -> List[dict]:
    combos = [{}]
    for key, values in axes.items():
        combos = [{**c, key: v} for c in combos for v in values]
    return combos


def build_grid() -> List[dict]:
    grid = []
    for combo in _product(_BREAKOUT_AXES):
        grid.append({"strategy": "breakout", **combo})
    for combo in _product(_BREAKOUT_AXES):
        for sma in (100, 200):
            grid.append({"strategy": "breakout_regime", "regime_sma": sma, **combo})
    for combo in _product(_PULLBACK_AXES):
        grid.append({"strategy": "pullback", **combo})
    return grid


def max_indicator_window(grid: List[dict]) -> int:
    """Largest completed-bar window any grid cell needs — drives how far back
    the sweep must fetch so every config has full indicators at train start
    (an SMA(200) config with insufficient history silently never trades,
    corrupting the train comparison)."""
    w = 1
    for cell in grid:
        w = max(w, cell.get("lookback", 0), cell.get("regime_sma", 0),
                cell.get("pullback_lookback", 0))
    return w


def run_cell(bars_by_symbol: Dict[str, List[Bar]], base_cfg: BacktestConfig,
            overrides: dict, start: date, end: date, risk_cfg) -> dict:
    """One grid cell over one window -> flat row of params + portfolio metrics."""
    cfg = replace(base_cfg, start=start, end=end, **overrides)
    result = run_backtest(bars_by_symbol, cfg, risk_cfg)
    trips = pair_round_trips(list(result.fills))
    p = summarize(result, trips)["portfolio"]
    row = {c: getattr(cfg, c) for c in PARAM_COLUMNS}
    row.update({
        "trades": p["trade_count"], "win_rate": p["win_rate"],
        "total_return": p["total_return"], "max_drawdown": p["max_drawdown"],
        "sharpe": p["sharpe"], "profit_factor": p["profit_factor"],
        "avg_win": p["avg_win"], "avg_loss": p["avg_loss"],
    })
    return row


def sweep(bars_by_symbol: Dict[str, List[Bar]], base_cfg: BacktestConfig,
         risk_cfg, grid: List[dict],
         train: Tuple[date, date], val: Tuple[date, date],
         max_dd: float = 0.05, min_train_trades: int = 30,
         min_val_trades: int = 8, top_k: int = 10) -> Tuple[List[dict], List[dict]]:
    """Pure orchestration: (all train rows incl. excluded_reason, final
    validation table ranked by validation win rate)."""
    train_rows = []
    for overrides in grid:
        row = run_cell(bars_by_symbol, base_cfg, overrides, train[0], train[1], risk_cfg)
        if row["trades"] < min_train_trades:
            row["excluded_reason"] = f"trades<{min_train_trades}"
        elif row["max_drawdown"] > max_dd:
            row["excluded_reason"] = f"max_dd>{max_dd}"
        else:
            row["excluded_reason"] = ""
        train_rows.append(row)

    # Top-K per variant family (multiple-testing control: one family can't
    # flood the validation stage).
    candidates = []
    for family in ("breakout", "breakout_regime", "pullback"):
        eligible = [r for r in train_rows
                    if r["strategy"] == family and not r["excluded_reason"]]
        eligible.sort(key=lambda r: (-r["win_rate"], -r["sharpe"]))
        candidates.extend(eligible[:top_k])

    topk_rows = []
    for train_row in candidates:
        overrides = {c: train_row[c] for c in PARAM_COLUMNS}
        val_row = run_cell(bars_by_symbol, base_cfg, overrides, val[0], val[1], risk_cfg)
        merged = {c: train_row[c] for c in PARAM_COLUMNS}
        merged.update({
            "train_win_rate": train_row["win_rate"],
            "train_max_dd": train_row["max_drawdown"],
            "train_sharpe": train_row["sharpe"],
            "val_trades": val_row["trades"],
            "val_win_rate": val_row["win_rate"],
            "val_max_dd": val_row["max_drawdown"],
            "val_sharpe": val_row["sharpe"],
            "val_return": val_row["total_return"],
            "val_profit_factor": val_row["profit_factor"],
            "val_avg_win": val_row["avg_win"],
            "val_avg_loss": val_row["avg_loss"],
            "gap": train_row["win_rate"] - val_row["win_rate"],
        })
        # Constraints count out-of-sample too.
        if merged["val_trades"] < min_val_trades:
            merged["val_excluded_reason"] = f"val_trades<{min_val_trades}"
        elif merged["val_max_dd"] > max_dd:
            merged["val_excluded_reason"] = f"val_max_dd>{max_dd}"
        else:
            merged["val_excluded_reason"] = ""
        topk_rows.append(merged)

    topk_rows.sort(key=lambda r: (r["val_excluded_reason"] != "",
                                  -r["val_win_rate"], -r["val_sharpe"]))
    return train_rows, topk_rows


def _write_csv(path: Path, rows: List[dict], columns: List[str]) -> None:
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=columns)
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in columns})


def _fmt_pct(x) -> str:
    return f"{x * 100:.1f}%"


def _print_topk(topk_rows: List[dict], max_dd: float) -> None:
    passed = [r for r in topk_rows if not r["val_excluded_reason"]]
    print()
    if passed:
        print(f"Validated configs (val max drawdown <= {max_dd * 100:.0f}%, ranked by "
             "VALIDATION win rate — 'gap' = train-val win-rate difference, an overfit indicator):")
        show = passed
    else:
        print("NO config met the constraints out-of-sample. Nearest misses "
             "(the honest frontier — each row names its violated constraint):")
        show = topk_rows[:10]
    hdr = (f"{'strategy':<17}{'params':<44}{'valWin%':>8}{'valDD%':>8}{'valPF':>7}"
          f"{'valShrp':>8}{'valTrd':>7}{'gap':>7}  {'violates':<20}")
    print(hdr)
    for r in show:
        params = (f"lb={r['lookback']} sma={r['regime_sma']} m={r['pullback_lookback']} "
                 f"d={r['pullback_depth_pct']} sl={r['stop_loss_pct']} "
                 f"tp={r['take_profit_pct']} tr={r['trailing_stop_pct']}")
        pf = f"{r['val_profit_factor']:.2f}" if r["val_profit_factor"] is not None else "n/a"
        print(f"{r['strategy']:<17}{params:<44}{r['val_win_rate'] * 100:>7.1f}%"
             f"{r['val_max_dd'] * 100:>7.2f}%{pf:>7}{r['val_sharpe']:>8.2f}"
             f"{r['val_trades']:>7}{r['gap'] * 100:>6.1f}%  {r['val_excluded_reason']:<20}")


def _utc_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m autotrader.backtest.sweep",
        description="Sweep strategy variants/parameters with out-of-sample validation.")
    p.add_argument("--symbols", default=DEFAULT_SYMBOLS)
    p.add_argument("--train-from", dest="train_from", type=date.fromisoformat,
                   default=date(2023, 1, 1))
    p.add_argument("--train-to", dest="train_to", type=date.fromisoformat,
                   default=date(2024, 12, 31))
    p.add_argument("--val-from", dest="val_from", type=date.fromisoformat,
                   default=date(2025, 1, 1))
    p.add_argument("--val-to", dest="val_to", type=date.fromisoformat, default=None,
                   help="Default: today")
    p.add_argument("--cash", type=float, default=100_000.0)
    p.add_argument("--commission", type=float, default=0.0)
    p.add_argument("--slippage-bps", type=float, default=0.0)
    p.add_argument("--max-dd", type=float, default=0.05)
    p.add_argument("--min-train-trades", type=int, default=30)
    p.add_argument("--min-val-trades", type=int, default=8)
    p.add_argument("--top-k", type=int, default=10)
    p.add_argument("--order-qty", type=int, default=int(os.getenv("ORDER_QTY", "1")))
    p.add_argument("--out-dir", default=None,
                   help="Default: ~/.autotrader/backtest_runs/<stamp>_sweep/")
    p.add_argument("--cache-dir", default=str(DEFAULT_CACHE_DIR))
    p.add_argument("--no-cache", action="store_true")
    p.add_argument("--api-key", default=os.getenv("MASSIVE_API_KEY", ""))
    return p


def main(argv=None, client_factory=None, today_fn=date.today) -> int:
    args = build_parser().parse_args(argv)
    val_to = args.val_to or today_fn()
    if not (args.train_from < args.train_to < args.val_from <= val_to):
        build_parser().error("windows must satisfy train-from < train-to < val-from <= val-to")

    symbols = tuple(sorted({normalize_ticker(s) for s in args.symbols.split(",") if s.strip()}))
    grid = build_grid()
    window = max_indicator_window(grid)
    fetch_start = args.train_from - timedelta(days=math.ceil(window * 1.6 + 10))
    factory = client_factory or (lambda: MassiveClient(args.api_key))

    bars_by_symbol = {}
    for sym in symbols:
        try:
            bars = load_bars(factory, Path(args.cache_dir), sym, 1, "day",
                             fetch_start.isoformat(), val_to.isoformat(),
                             use_cache=not args.no_cache)
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

    base_cfg = BacktestConfig(
        symbols=tuple(sorted(bars_by_symbol)), start=args.train_from, end=args.train_to,
        multiplier=1, timespan="day", cash=args.cash,
        commission_per_order=args.commission, slippage_bps=args.slippage_bps,
        order_qty=args.order_qty,
    )
    risk_cfg = load_risk_config()

    print(f"Sweep: {len(grid)} configs x {len(bars_by_symbol)} symbols   "
         f"train {args.train_from}->{args.train_to}   val {args.val_from}->{val_to}")
    train_rows, topk_rows = sweep(
        bars_by_symbol, base_cfg, risk_cfg, grid,
        (args.train_from, args.train_to), (args.val_from, val_to),
        max_dd=args.max_dd, min_train_trades=args.min_train_trades,
        min_val_trades=args.min_val_trades, top_k=args.top_k)

    excluded = sum(1 for r in train_rows if r["excluded_reason"])
    print(f"Train grid: {len(train_rows)} configs, {excluded} excluded "
         f"(low trades or max_dd > {args.max_dd * 100:.0f}%), "
         f"{len(topk_rows)} candidates validated")
    _print_topk(topk_rows, args.max_dd)

    out_dir = Path(args.out_dir) if args.out_dir else (DEFAULT_RUNS_DIR / f"{_utc_stamp()}_sweep")
    out_dir.mkdir(parents=True, exist_ok=True)
    _write_csv(out_dir / "train_grid.csv", train_rows,
               PARAM_COLUMNS + METRIC_COLUMNS + ["excluded_reason"])
    topk_columns = PARAM_COLUMNS + ["train_win_rate", "train_max_dd", "train_sharpe",
                                    "val_trades", "val_win_rate", "val_max_dd",
                                    "val_sharpe", "val_return", "val_profit_factor",
                                    "val_avg_win", "val_avg_loss", "gap",
                                    "val_excluded_reason"]
    _write_csv(out_dir / "validation_topk.csv", topk_rows, topk_columns)
    print(f"\nWrote {out_dir}/train_grid.csv and validation_topk.csv")

    # Run of record for the best VALIDATED config, over the validation window
    # only (full period would mix in-sample data into the headline numbers).
    winners = [r for r in topk_rows if not r["val_excluded_reason"]]
    if winners:
        best = winners[0]
        overrides = {c: best[c] for c in PARAM_COLUMNS}
        cfg = replace(base_cfg, start=args.val_from, end=val_to, **overrides)
        result = run_backtest(bars_by_symbol, cfg, risk_cfg)
        trips = pair_round_trips(list(result.fills))
        summary = summarize(result, trips)
        # With an explicit --out-dir, EVERYTHING stays under it (tests and
        # scripted runs must never write into the real home runs dir); only
        # the default path lands in DEFAULT_RUNS_DIR where the dashboard looks.
        if args.out_dir:
            record_dir = out_dir / f"best-{best['strategy']}"
        else:
            record_dir = DEFAULT_RUNS_DIR / f"{_utc_stamp()}_sweep-best-{best['strategy']}"
        record_dir.mkdir(parents=True, exist_ok=True)
        write_html_report(result, summary, trips, record_dir / "report.html")
        _write_equity_csv(result, record_dir / "equity_curve.csv")
        _write_trades_csv(result, record_dir / "trades.csv")
        import json
        (record_dir / "summary.json").write_text(json.dumps(summary, indent=2),
                                                 encoding="utf-8")
        print(f"Run of record (best validated config, validation window): {record_dir}")
        _print_summary(cfg, summary)
    else:
        print("\nNo run of record written — no config met the constraints out-of-sample.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
