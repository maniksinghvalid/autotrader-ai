"""Stateless pullback (mean-reversion) strategy. Pure:
(price, position, ref_low, sma) -> Signal | None.

Buys dips within an uptrend: enters long only when price is at/below the
M-day reference low (or a depth below the SMA — the reference computation
belongs to the caller, like BreakoutReference does for breakout) AND the
regime filter holds (price above the R-day SMA). The high-win-rate shape:
small take-profit vs. the stop, many small winners, occasional larger
losers. Exits on stop / target via the shared helper. Fail-safe: ref_low or
sma None (no data) => NO entry (CLAUDE.md: explicit stop + target; no state
between ticks)."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from autotrader.domain import Position, Signal
from autotrader.strategies.exits import manage_long_exit


@dataclass(frozen=True)
class PullbackParams:
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


class PullbackStrategy:
    def __init__(self, params: PullbackParams):
        self.p = params

    def evaluate(self, price: float, position: Optional[Position],
                 ref_low: Optional[float] = None,
                 sma: Optional[float] = None) -> Optional[Signal]:
        held = position.qty if position else 0
        if held > 0:
            return manage_long_exit(self.p.symbol, price, position,
                                    self.p.stop_loss_pct, self.p.take_profit_pct,
                                    self.p.confidence)
        # Flat: buy the dip ONLY in an uptrend. Missing either reference => no entry.
        if (held == 0 and sma is not None and price > sma
                and ref_low is not None and price <= ref_low):
            return Signal(self.p.symbol, "BUY", self.p.confidence,
                          f"pullback: {price} <= {self.p.symbol} ref low {ref_low} "
                          f"in uptrend (> sma {sma})")
        return None
