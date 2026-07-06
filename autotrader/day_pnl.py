"""Robust intraday P&L for when the broker cannot supply it (moomoo paper accounts
return realized_pl='N/A'). Realized comes from our own fill ledger (avg-cost,
today's sells only — see reporting.pnl.realized_from_fills); unrealized is marked
from live quotes. Shared by the engine's risk check and the runner's EOD perf
recording so the two computations cannot drift. Pure: no SDK, no DB, no clock."""
from __future__ import annotations

from typing import Callable, Optional


def unrealized_from_quotes(positions, get_quote: Callable[[str], Optional[float]]) -> float:
    """Σ qty × (quote − avg_price) over open positions that have a live quote.
    Positions with no quote are skipped (marked at cost)."""
    total = 0.0
    for p in positions:
        quote = get_quote(p.symbol)
        if quote is not None:
            total += p.qty * (quote - p.avg_price)
    return total
