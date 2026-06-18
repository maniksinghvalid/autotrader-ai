"""Pure capital-flow figures for the EOD header, from a day's fills. Options use
the x100 multiplier; stock x1. Never raises."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Tuple

from autotrader.reporting.option_code import parse_option_code


@dataclass(frozen=True)
class CapitalFlow:
    premium_collected: float   # SELL option premium, positive
    premium_paid: float        # BUY option premium, positive
    net_cash_deployed: float   # signed over all legs (SELL +, BUY -)


def compute_capital_flow(fills: Iterable[Tuple[str, str, float, float]]) -> CapitalFlow:
    collected = paid = net = 0.0
    for symbol, side, qty, price in fills:
        parsed = parse_option_code(symbol)
        mult = parsed.multiplier if parsed is not None else 1
        dollars = qty * price * mult
        net += dollars if side == "SELL" else -dollars
        if parsed is not None:
            if side == "SELL":
                collected += dollars
            else:
                paid += dollars
    return CapitalFlow(premium_collected=collected, premium_paid=paid,
                       net_cash_deployed=net)
