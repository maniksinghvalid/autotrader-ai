"""Pure option-contract selection. Given a chain snapshot (list of OptionQuote),
pick the contract closest to a target delta within a DTE window (D5). No I/O:
the broker supplies the snapshot; this module just chooses. Imports domain only."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import List, Optional

from autotrader.domain import OptionContract, OptionRight


@dataclass(frozen=True)
class OptionQuote:
    """One row of an option chain with the greeks/price the selector needs."""
    code: str
    underlying: str
    expiry: date
    strike: float
    right: OptionRight
    delta: float
    premium: float   # mid premium per share (x multiplier = contract cost)

    def __post_init__(self):
        if self.right not in ("CALL", "PUT"):
            raise ValueError(f"OptionQuote.right must be CALL/PUT, got {self.right!r}")


def select_contract(quotes: List[OptionQuote], right: OptionRight,
                    target_delta: float, dte_min: int, dte_max: int,
                    asof: date, pin_expiry: Optional[date] = None) -> Optional[OptionQuote]:
    """Closest-to-target-|delta| contract of `right` whose DTE is in
    [dte_min, dte_max] and premium > 0. When `pin_expiry` is set, candidates are
    further restricted to that exact expiry (used to keep multi-leg single-expiry
    strategies on one expiry). None if nothing qualifies. Ties broken by nearest
    expiry, then lowest strike (deterministic regardless of input order)."""
    target = abs(target_delta)
    candidates = [
        q for q in quotes
        if q.right == right and q.premium > 0
        and dte_min <= (q.expiry - asof).days <= dte_max
        and (pin_expiry is None or q.expiry == pin_expiry)
    ]
    if not candidates:
        return None
    return min(candidates, key=lambda q: (abs(abs(q.delta) - target),
                                          (q.expiry - asof).days, q.strike))


def to_contract(q: OptionQuote) -> OptionContract:
    return OptionContract(underlying=q.underlying, expiry=q.expiry,
                          strike=q.strike, right=q.right, code=q.code,
                          multiplier=100)
