"""Stateless threshold strategy. Pure: (price, position) -> Signal | None.
Declares an explicit stop-loss AND take-profit (CLAUDE.md). Holds no state
between ticks; all parameters are frozen at construction. Never touches the
broker or router — its output is a data value routed by main.py."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from autotrader.domain import Position, Signal


@dataclass(frozen=True)
class StrategyParams:
    symbol: str
    entry_price: float        # buy when price >= entry_price and flat
    stop_loss_pct: float      # e.g. 0.05 = exit at -5% from avg_price
    take_profit_pct: float    # e.g. 0.10 = exit at +10% from avg_price
    confidence: float

    def __post_init__(self):
        if self.stop_loss_pct <= 0:
            raise ValueError("stop_loss_pct must be > 0 (explicit stop required)")
        if self.take_profit_pct <= 0:
            raise ValueError("take_profit_pct must be > 0 (explicit target required)")
        if not (0.0 <= self.confidence <= 1.0):
            raise ValueError("confidence must be in [0,1]")


class ThresholdStrategy:
    def __init__(self, params: StrategyParams):
        self.p = params

    def evaluate(self, price: float, position: Optional[Position]) -> Optional[Signal]:
        held = position.qty if position else 0
        # Manage an open long: exit on stop or target.
        if held > 0 and position is not None:
            change = (price - position.avg_price) / position.avg_price
            if change <= -self.p.stop_loss_pct:
                return Signal(self.p.symbol, "SELL", self.p.confidence,
                              f"stop-loss hit ({change:.2%})")
            if change >= self.p.take_profit_pct:
                return Signal(self.p.symbol, "SELL", self.p.confidence,
                              f"take-profit hit ({change:.2%})")
            return None
        # Flat: enter on threshold cross.
        if held == 0 and price >= self.p.entry_price:
            return Signal(self.p.symbol, "BUY", self.p.confidence,
                          f"price {price} >= entry {self.p.entry_price}")
        return None
