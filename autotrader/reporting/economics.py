"""Pure per-strategy economics from a group's parsed legs + an optional basis
price. Every figure is Optional; missing inputs yield None. Never raises."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Sequence

from autotrader.reporting.classify import Leg


@dataclass(frozen=True)
class StrategyEconomics:
    net_premium: Optional[float]    # signed $ over option legs (SELL +, BUY -)
    floor: Optional[float]          # lowest long-put strike
    cap: Optional[float]            # lowest short-call strike
    floor_pct: Optional[float]      # (floor-basis)/basis*100
    cap_pct: Optional[float]        # (cap-basis)/basis*100
    hedge_cost_pct: Optional[float] # long-put premium / basis * 100


def _pct(strike: Optional[float], basis: Optional[float]) -> Optional[float]:
    if strike is None or not basis:
        return None
    return (strike - basis) / basis * 100.0


def compute_economics(legs: Sequence[Leg], basis: Optional[float]) -> StrategyEconomics:
    option_legs = [l for l in legs if l.option is not None]
    net_premium = None
    if option_legs:
        net_premium = sum(
            (1.0 if l.side == "SELL" else -1.0) * l.qty * l.price * l.option.multiplier
            for l in option_legs)
    long_puts = [l for l in option_legs if l.option.right == "PUT" and l.side == "BUY"]
    short_calls = [l for l in option_legs if l.option.right == "CALL" and l.side == "SELL"]
    floor = min((l.option.strike for l in long_puts), default=None)
    cap = min((l.option.strike for l in short_calls), default=None)
    hedge_cost_pct = None
    if long_puts and basis:
        hedge_cost_pct = long_puts[0].price / basis * 100.0
    return StrategyEconomics(
        net_premium=net_premium, floor=floor, cap=cap,
        floor_pct=_pct(floor, basis), cap_pct=_pct(cap, basis),
        hedge_cost_pct=hedge_cost_pct)
