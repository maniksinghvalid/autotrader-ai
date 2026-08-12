"""Pure performance math over a BacktestResult. Stdlib only (math/statistics)."""
from __future__ import annotations

import math
import statistics
from dataclasses import dataclass
from typing import Dict, List, Optional

from autotrader.backtest.engine import BacktestResult, Fill

_TRADING_DAYS_PER_YEAR = 252


@dataclass(frozen=True)
class RoundTrip:
    symbol: str
    entry_ts: int
    exit_ts: int
    qty: int
    entry_price: float
    exit_price: float
    commissions: float
    pnl: float
    exit_reason: str


def pair_round_trips(fills: List[Fill]) -> List[RoundTrip]:
    """BUY -> SELL per symbol. The engine only ever holds one lot per symbol
    at a time (full-position entries and exits, no pyramiding), so a simple
    "most recent open BUY" pairing is exact. A BUY with no matching SELL
    (still open at the end of the run) is excluded — it isn't a completed trade."""
    trips = []
    open_buy: Dict[str, Fill] = {}
    for f in sorted(fills, key=lambda x: x.ts):
        if f.side == "BUY":
            open_buy[f.symbol] = f
        else:
            b = open_buy.pop(f.symbol, None)
            if b is None:
                continue
            pnl = (f.price - b.price) * f.qty - b.commission - f.commission
            trips.append(RoundTrip(
                symbol=f.symbol, entry_ts=b.ts, exit_ts=f.ts, qty=f.qty,
                entry_price=b.price, exit_price=f.price,
                commissions=b.commission + f.commission, pnl=pnl,
                exit_reason=f.reason))
    return trips


def max_drawdown(values: List[float]) -> float:
    """Largest peak-to-trough decline as a fraction (0.25 = 25%)."""
    peak = None
    worst = 0.0
    for v in values:
        if peak is None or v > peak:
            peak = v
        if peak and peak > 0:
            worst = max(worst, (peak - v) / peak)
    return worst


def sharpe(daily_returns: List[float]) -> float:
    """Annualized Sharpe (rf=0) from daily returns. 0.0 on n<2 or zero variance
    rather than raising — a flat/degenerate series has no meaningful ratio."""
    if len(daily_returns) < 2:
        return 0.0
    stdev = statistics.stdev(daily_returns)
    if stdev == 0:
        return 0.0
    return statistics.mean(daily_returns) / stdev * math.sqrt(_TRADING_DAYS_PER_YEAR)


def cagr(initial: float, final: float, days: int) -> float:
    if days <= 0 or initial <= 0 or final <= 0:
        return 0.0
    return (final / initial) ** (365.25 / days) - 1.0


def _trade_stats(trips: List[RoundTrip]) -> dict:
    wins = [t.pnl for t in trips if t.pnl > 0]
    losses = [t.pnl for t in trips if t.pnl <= 0]
    gross_win = sum(wins)
    gross_loss = -sum(losses)
    return {
        "trade_count": len(trips),
        "win_rate": (len(wins) / len(trips)) if trips else 0.0,
        "avg_win": (gross_win / len(wins)) if wins else 0.0,
        "avg_loss": (sum(losses) / len(losses)) if losses else 0.0,
        "profit_factor": (gross_win / gross_loss) if gross_loss > 0 else None,
        "realized_pnl": sum(t.pnl for t in trips),
    }


def summarize(result: BacktestResult, trips: List[RoundTrip]) -> dict:
    cfg = result.cfg
    initial = cfg.cash
    curve = result.equity_curve
    final_value = curve[-1][1] if curve else initial
    days = (curve[-1][0] - curve[0][0]).days if len(curve) >= 2 else 0
    values = [v for _, v, _ in curve]
    daily_returns = [(values[i] - values[i - 1]) / values[i - 1]
                     for i in range(1, len(values)) if values[i - 1] != 0]
    total_days = len(curve)
    exposure_days = sum(1 for _, v, c in curve if abs(v - c) > 1e-9)

    portfolio = {
        "total_return": (final_value / initial - 1.0) if initial > 0 else 0.0,
        "cagr": cagr(initial, final_value, days),
        "sharpe": sharpe(daily_returns),
        "max_drawdown": max_drawdown(values) if values else 0.0,
        "final_value": final_value,
        "exposure": (exposure_days / total_days) if total_days else 0.0,
        "total_commission": sum(f.commission for f in result.fills),
        "skipped_for_cash": result.skipped_for_cash,
    }
    portfolio.update(_trade_stats(trips))

    per_symbol = {}
    for sym in cfg.symbols:
        sym_trips = [t for t in trips if t.symbol == sym]
        sym_stats = _trade_stats(sym_trips)
        sym_stats["exposure"] = (result.held_days.get(sym, 0) / total_days) if total_days else 0.0
        per_symbol[sym] = sym_stats

    return {"portfolio": portfolio, "per_symbol": per_symbol}
