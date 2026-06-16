"""Pure, stateless risk-per-trade position sizing for BUY entries.

No Moomoo/SDK imports — this module must import with no OpenD present (mirrors
risk_core.py and normalize.py). It only PROPOSES a quantity; risk_core.evaluate
remains the sole pass/reject gate. The sizer never raises a quantity above the
risk-derived base and never approves an order — it clamps DOWN to cap headroom
so a sane risk-size is not needlessly rejected by a hard cap.

Formula (BUY only; SELL stays full-liquidation and never calls this):

    stop_dist = entry - signal_stop          # else entry * (trailing_stop_pct/100)
    base      = floor(equity * risk_per_trade_pct / stop_dist)
    factor    = floor..ceil remapped over [min_confidence, 1.0]
    qty       = floor(base * factor)         # then clamped to cap headroom
"""
from __future__ import annotations

import logging
import math
from dataclasses import dataclass

from autotrader.config import RiskConfig

logger = logging.getLogger("autotrader.sizing")


@dataclass(frozen=True)
class SizeResult:
    qty: int
    reason: str
    used_risk_sizing: bool


def _finite_pos(x: float) -> bool:
    return isinstance(x, (int, float)) and math.isfinite(x) and x > 0


def size_position(*, equity: float, entry_price: float, signal_stop,
                  confidence: float, cfg: RiskConfig, current_qty: int,
                  gross_exposure: float, fixed_qty: int) -> SizeResult:
    # 0. Default-off: sizing disabled -> use the fixed ORDER_QTY path unchanged.
    if cfg.risk_per_trade_pct <= 0:
        return SizeResult(fixed_qty, "FIXED_DISABLED", False)

    # 1. Guard the account/market inputs first (stale snapshot, zero/bad quote):
    #    a non-finite equity or price is never tradable, regardless of the stop.
    if not _finite_pos(equity) or not _finite_pos(entry_price):
        return SizeResult(0, "SIZED_ZERO_BAD_INPUTS", True)

    # 2. Stop distance: a valid signal hard stop wins; else trailing_stop_pct; else none.
    if (signal_stop is not None and math.isfinite(signal_stop)
            and 0 < signal_stop < entry_price):
        stop_dist = entry_price - signal_stop
    elif cfg.trailing_stop_pct > 0:
        stop_dist = entry_price * (cfg.trailing_stop_pct / 100.0)
    else:
        return SizeResult(fixed_qty, "NO_STOP_DISTANCE_FALLBACK", False)
    if not math.isfinite(stop_dist) or stop_dist <= 0:
        return SizeResult(fixed_qty, "NO_STOP_DISTANCE_FALLBACK", False)

    # 3. Risk-based base quantity.
    base = math.floor(equity * cfg.risk_per_trade_pct / stop_dist)

    # 4. Confidence factor: remap [min_confidence, 1.0] -> [floor, ceil], clamped.
    #    A degenerate span (min_confidence == 1.0) means only max-confidence signals
    #    trade, so they get the ceiling (full size), not the floor.
    span = 1.0 - cfg.min_confidence
    t = 1.0 if span <= 1e-9 else max(0.0, min(1.0, (confidence - cfg.min_confidence) / span))
    factor = cfg.confidence_size_floor + t * (cfg.confidence_size_ceil - cfg.confidence_size_floor)
    qty = math.floor(base * factor)

    # 5. Clamp DOWN to cap headroom (never up). risk_core stays the final gate.
    qty = min(qty, cfg.max_position_qty - current_qty)
    qty = min(qty, math.floor(cfg.max_order_notional / entry_price))
    qty = min(qty, math.floor((cfg.max_gross_exposure - gross_exposure) / entry_price))

    if qty <= 0:
        return SizeResult(0, "SIZED_ZERO", True)
    return SizeResult(qty, "SIZED", True)
