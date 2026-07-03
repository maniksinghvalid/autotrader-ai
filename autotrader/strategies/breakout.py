"""Stateless N-day breakout strategy. Pure: (price, position, ref_high) -> Signal | None.
Enters long only on a new N-day high (price > ref_high); the reference high is computed
by BreakoutReference and passed in — the strategy never fetches data. Exits on stop /
target via the shared helper. Fail-safe: ref_high None (no data) => NO entry, never
buy-at-open (CLAUDE.md: explicit stop + target; no state between ticks)."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from autotrader.domain import Position, Signal
from autotrader.strategies.exits import manage_long_exit


@dataclass(frozen=True)
class BreakoutParams:
    symbol: str
    stop_loss_pct: float
    take_profit_pct: float
    confidence: float

    def __post_init__(self):
        if self.stop_loss_pct <= 0:
            raise ValueError("stop_loss_pct must be > 0 (explicit stop required)")
        if self.take_profit_pct <= 0:
            raise ValueError("take_profit_pct must be > 0 (explicit target required)")
        if not (0.0 <= self.confidence <= 1.0):
            raise ValueError("confidence must be in [0,1]")


class BreakoutStrategy:
    def __init__(self, params: BreakoutParams):
        self.p = params

    def evaluate(self, price: float, position: Optional[Position],
                 ref_high: Optional[float] = None) -> Optional[Signal]:
        held = position.qty if position else 0
        if held > 0:
            return manage_long_exit(self.p.symbol, price, position,
                                    self.p.stop_loss_pct, self.p.take_profit_pct,
                                    self.p.confidence)
        # Flat: enter ONLY on a confirmed breakout. No reference => no entry.
        if held == 0 and ref_high is not None and price > ref_high:
            return Signal(self.p.symbol, "BUY", self.p.confidence,
                          f"breakout: {price} > {self.p.symbol} high {ref_high}")
        return None
