"""Stateless threshold strategy. Pure: (price, position) -> Signal | None.
Declares an explicit stop-loss AND take-profit (CLAUDE.md). Holds no state
between ticks; all parameters are frozen at construction. Never touches the
broker or router — its output is a data value routed by main.py."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from autotrader.domain import Position, Signal
from autotrader.strategies.exits import manage_long_exit


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

    def evaluate(self, price: float, position: Optional[Position],
                 ref_high: Optional[float] = None) -> Optional[Signal]:
        # ref_high is accepted for a uniform engine call path (see BreakoutStrategy)
        # and ignored here — this strategy enters on an absolute threshold.
        held = position.qty if position else 0
        if held > 0:
            return manage_long_exit(self.p.symbol, price, position,
                                    self.p.stop_loss_pct, self.p.take_profit_pct,
                                    self.p.confidence)
        if held == 0 and price >= self.p.entry_price:
            return Signal(self.p.symbol, "BUY", self.p.confidence,
                          f"price {price} >= entry {self.p.entry_price}")
        return None
