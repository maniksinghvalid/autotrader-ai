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


def select_contract(quotes: List[OptionQuote], right: OptionRight,
                    target_delta: float, dte_min: int, dte_max: int,
                    asof: date) -> Optional[OptionQuote]:
    """Closest-to-target-|delta| contract of `right` whose DTE is in
    [dte_min, dte_max] and premium > 0. None if nothing qualifies."""
    target = abs(target_delta)
    candidates = [
        q for q in quotes
        if q.right == right and q.premium > 0
        and dte_min <= (q.expiry - asof).days <= dte_max
    ]
    if not candidates:
        return None
    return min(candidates, key=lambda q: abs(abs(q.delta) - target))


def to_contract(q: OptionQuote) -> OptionContract:
    return OptionContract(underlying=q.underlying, expiry=q.expiry,
                          strike=q.strike, right=q.right, code=q.code,
                          multiplier=100)
