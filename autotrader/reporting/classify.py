"""Pure structural classifier: label a single underlying's day of legs as a
known options strategy. Authoritative (doesn't read signal text). Never raises."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Sequence

from autotrader.reporting.option_code import ParsedOption


@dataclass(frozen=True)
class Leg:
    side: str                       # "BUY" | "SELL"
    qty: float
    price: float
    option: Optional[ParsedOption]  # None = stock leg


def classify_strategy(legs: Sequence[Leg]) -> str:
    stock = [l for l in legs if l.option is None]
    calls = [l for l in legs if l.option is not None and l.option.right == "CALL"]
    puts = [l for l in legs if l.option is not None and l.option.right == "PUT"]
    has_stock = bool(stock)
    short_call = any(c.side == "SELL" for c in calls)
    long_call = any(c.side == "BUY" for c in calls)
    long_put = any(p.side == "BUY" for p in puts)
    short_put = any(p.side == "SELL" for p in puts)

    if has_stock and short_call and long_put:
        return "Collar"
    if has_stock and short_call and not puts:
        return "Covered Call"
    if has_stock and long_put and not calls:
        return "Protective Put"
    if not has_stock and long_put and short_put:
        return "Bear Put Spread"
    if not has_stock and not puts:
        if len(calls) >= 2:
            return "PMCC / Call Diagonal"
        if len(calls) == 1 and long_call:
            return "LEAP"
        if len(calls) == 1 and short_call:
            return "Covered Call (existing shares)"
    if has_stock and not calls and not puts:
        return "Stock entry" if stock[0].side == "BUY" else "Stock exit"
    return "Strategy"
