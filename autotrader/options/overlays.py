"""Overlay registry: each overlay is a stateless structural definition (its legs
+ whether it needs underlying shares). Concrete strike/expiry come from the chain
selector and config at plan time; the exit rule is attached from config by the
planner. O1 registers the two single-leg, share-covered overlays. Adding COLLAR /
spreads later is a new entry here — no schema or enum change. Imports domain only."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Tuple

from autotrader.domain import OptionRight, OverlayType, PositionEffect, Side


@dataclass(frozen=True)
class LegSpec:
    """One leg of an overlay, pre-contract-resolution. `ratio` is contracts per
    100 shares of underlying (1 = one contract per round lot)."""
    right: OptionRight
    side: Side
    position_effect: PositionEffect = "OPEN"
    ratio: int = 1

    def __post_init__(self):
        if self.right not in ("CALL", "PUT"):
            raise ValueError(f"LegSpec.right must be CALL/PUT, got {self.right!r}")
        if self.side not in ("BUY", "SELL"):
            raise ValueError(f"LegSpec.side must be BUY/SELL, got {self.side!r}")
        if self.position_effect not in ("OPEN", "CLOSE"):
            raise ValueError(f"LegSpec.position_effect must be OPEN/CLOSE, got {self.position_effect!r}")
        if self.ratio < 1:
            raise ValueError(f"LegSpec.ratio must be >= 1, got {self.ratio}")


@dataclass(frozen=True)
class ExitRule:
    """Declared exit discipline (CLAUDE.md). Values sourced from config at plan
    time; ENFORCEMENT (the close scan) is O4 — O1 only records the declaration."""
    dte_to_close: int
    profit_target_pct: float
    close_on_reversal: bool = True


@dataclass(frozen=True)
class OverlayDef:
    requires_underlying: bool
    legs: Tuple[LegSpec, ...]


REGISTRY: Dict[OverlayType, OverlayDef] = {
    OverlayType.COVERED_CALL: OverlayDef(
        requires_underlying=True,
        legs=(LegSpec(right="CALL", side="SELL"),),
    ),
    OverlayType.PROTECTIVE_PUT: OverlayDef(
        requires_underlying=True,
        legs=(LegSpec(right="PUT", side="BUY"),),
    ),
}
