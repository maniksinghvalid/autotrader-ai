"""Normalize a validated RoutineSignalPayload into neutral domain.Signal values.

Mapping (deterministic, no I/O):
  direction:  UP -> BUY, DOWN -> SELL
  symbol:     bare 'AAPL' -> 'US.AAPL'; an already-qualified 'US.AAPL' passes
              through; always upper-cased
  confidence: |points_delta| scaled by confidence_scale and clamped to [0, 1]
              (points_delta >= scale -> 1.0; points_delta 0 -> 0.0, which the
              confidence filter drops as an explicit no-trade).

confidence_scale is a signal-shaping parameter (NOT a risk limit), so it is a
function argument with a default — not a RiskConfig field."""
from __future__ import annotations

from typing import List

from autotrader.domain import Signal
from autotrader.signals.schema import RoutineSignalPayload, SignalChange

_DIRECTION = {"UP": "BUY", "DOWN": "SELL"}


def _normalize_symbol(ticker: str) -> str:
    t = ticker.strip().upper()
    return t if "." in t else f"US.{t}"


def _confidence(points_delta: int, scale: float) -> float:
    if scale <= 0:
        return 0.0
    return max(0.0, min(1.0, abs(points_delta) / scale))


def normalize_change(change: SignalChange, confidence_scale: float = 10.0) -> Signal:
    return Signal(
        symbol=_normalize_symbol(change.ticker),
        direction=_DIRECTION[change.direction],
        confidence=_confidence(change.points_delta, confidence_scale),
        rationale=f"{change.driver or 'external'}: "
                  f"{'/'.join(change.transition) or change.direction}",
    )


def normalize_payload(payload: RoutineSignalPayload,
                      confidence_scale: float = 10.0) -> List[Signal]:
    return [normalize_change(c, confidence_scale) for c in payload.signal_changes]
