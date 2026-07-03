"""Overlay registry: each overlay is a stateless structural definition (its legs
+ whether it needs underlying shares). Concrete strike/expiry come from the chain
selector and config at plan time; the exit rule is attached from config by the
planner. O1 registers the two single-leg, share-covered overlays. Adding COLLAR /
spreads later is a new entry here — no schema or enum change. Imports domain only."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Optional, Tuple

from autotrader.domain import OptionRight, OverlayType, PositionEffect, Side


_UNSET = object()  # "not overridden" marker; None is a valid explicit override


@dataclass(frozen=True)
class LegSpec:
    """One leg of an overlay, pre-contract-resolution. `ratio` is contracts per
    100 shares of underlying (1 = one contract per round lot). target_delta /
    dte_min / dte_max are per-leg selection overrides; when None the planner
    falls back to the global config (cfg.option_target_delta / dte_min / dte_max).
    target_delta uses the absolute-value convention: both puts and calls pass a
    positive delta (e.g. 0.30 for a 30-delta put), mirroring the chain selector's
    abs(delta) comparison."""
    right: OptionRight
    side: Side
    position_effect: PositionEffect = "OPEN"
    ratio: int = 1
    target_delta: Optional[float] = None
    dte_min: Optional[int] = None
    dte_max: Optional[int] = None
    prefer_longest: bool = False

    def __post_init__(self):
        if self.right not in ("CALL", "PUT"):
            raise ValueError(f"LegSpec.right must be CALL/PUT, got {self.right!r}")
        if self.side not in ("BUY", "SELL"):
            raise ValueError(f"LegSpec.side must be BUY/SELL, got {self.side!r}")
        if self.position_effect not in ("OPEN", "CLOSE"):
            raise ValueError(f"LegSpec.position_effect must be OPEN/CLOSE, got {self.position_effect!r}")
        if self.ratio < 1:
            raise ValueError(f"LegSpec.ratio must be >= 1, got {self.ratio}")
        if self.target_delta is not None and not (0.0 < self.target_delta <= 1.0):
            raise ValueError(f"LegSpec.target_delta must be in (0,1], got {self.target_delta}")
        if (self.dte_min is not None and self.dte_max is not None
                and self.dte_min > self.dte_max):
            raise ValueError(f"LegSpec dte_min {self.dte_min} > dte_max {self.dte_max}")
        if self.dte_min is not None and self.dte_min < 0:
            raise ValueError(f"LegSpec.dte_min must be >= 0, got {self.dte_min}")
        if self.dte_max is not None and self.dte_max < 0:
            raise ValueError(f"LegSpec.dte_max must be >= 0, got {self.dte_max}")


@dataclass(frozen=True)
class ExitRule:
    """Declared exit discipline (CLAUDE.md). Values sourced from config at plan
    time; ENFORCEMENT (the close scan) is O4 — O1 only records the declaration."""
    dte_to_close: int
    profit_target_pct: Optional[float]
    close_on_reversal: bool = True


@dataclass(frozen=True)
class OverlayDef:
    requires_underlying: bool
    legs: Tuple[LegSpec, ...]
    single_expiry: bool = False
    opens_stock: bool = False              # True => planner prepends a BUY equity anchor leg
    dte_to_close: object = _UNSET          # int override, or _UNSET to use cfg
    profit_target_pct: object = _UNSET     # float|None override, or _UNSET to use cfg


REGISTRY: Dict[OverlayType, OverlayDef] = {
    OverlayType.COVERED_CALL: OverlayDef(
        requires_underlying=True,
        legs=(LegSpec(right="CALL", side="SELL"),),
    ),
    OverlayType.PROTECTIVE_PUT: OverlayDef(
        requires_underlying=True,
        legs=(LegSpec(right="PUT", side="BUY"),),
    ),
    # Anchor leg (legs[0]) is always the covering LONG leg; it is selected first
    # and submitted first. single_expiry pins later legs to the anchor's expiry.
    OverlayType.COLLAR: OverlayDef(
        requires_underlying=True,
        single_expiry=True,
        legs=(
            LegSpec(right="PUT", side="BUY", target_delta=0.30, dte_min=30, dte_max=45),
            LegSpec(right="CALL", side="SELL", target_delta=0.30, dte_min=30, dte_max=45),
        ),
    ),
    OverlayType.BEAR_PUT_SPREAD: OverlayDef(
        requires_underlying=False,
        single_expiry=True,
        legs=(
            LegSpec(right="PUT", side="BUY", target_delta=0.45, dte_min=30, dte_max=45),
            LegSpec(right="PUT", side="SELL", target_delta=0.25, dte_min=30, dte_max=45),
        ),
    ),
    OverlayType.CALL_DIAGONAL: OverlayDef(
        requires_underlying=False,
        single_expiry=False,
        legs=(
            LegSpec(right="CALL", side="BUY", target_delta=0.80, dte_min=180, dte_max=365),
            LegSpec(right="CALL", side="SELL", target_delta=0.30, dte_min=30, dte_max=45),
        ),
    ),
    OverlayType.LEAP: OverlayDef(
        requires_underlying=False,
        single_expiry=False,
        dte_to_close=120,            # roll ~4 months out (below the 180 buy floor)
        profit_target_pct=None,      # long stock-replacement: ride it
        legs=(
            LegSpec(right="CALL", side="BUY", target_delta=0.80,
                    dte_min=180, dte_max=730, prefer_longest=True),
        ),
    ),
}
