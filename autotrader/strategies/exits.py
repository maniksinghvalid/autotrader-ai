"""Shared long-exit rule for the internal strategies: stop-loss / take-profit off
avg cost. One place so BreakoutStrategy and ThresholdStrategy cannot drift."""
from __future__ import annotations

from typing import Optional

from autotrader.domain import Position, Signal


def manage_long_exit(symbol: str, price: float, position: Optional[Position],
                     stop_loss_pct: float, take_profit_pct: float,
                     confidence: float) -> Optional[Signal]:
    """SELL Signal when an open long hits its stop or target, else None (including
    when flat — the caller owns the entry rule)."""
    held = position.qty if position else 0
    if held <= 0 or position is None:
        return None
    change = (price - position.avg_price) / position.avg_price
    if change <= -stop_loss_pct:
        return Signal(symbol, "SELL", confidence, f"stop-loss hit ({change:.2%})")
    if change >= take_profit_pct:
        return Signal(symbol, "SELL", confidence, f"take-profit hit ({change:.2%})")
    return None
