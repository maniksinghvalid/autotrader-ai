"""Pure structural classifier: label a single underlying's day of legs as a
known options strategy. Authoritative (doesn't read signal text). Never raises."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Sequence

from autotrader.reporting.option_code import ParsedOption


@dataclass(frozen=True)
class Leg:
    side: str                       # "BUY" | "SELL"
    qty: float
    price: float
    option: Optional[ParsedOption]  # None = stock leg


def classify_strategy(legs: Sequence[Leg]) -> str:
    stock = [l for l in legs if l.option is None]
    calls = [l for l in legs if l.option is not None and l.option.right == "CALL"]
    puts = [l for l in legs if l.option is not None and l.option.right == "PUT"]
    has_stock = bool(stock)
    short_call = any(c.side == "SELL" for c in calls)
    long_call = any(c.side == "BUY" for c in calls)
    long_put = any(p.side == "BUY" for p in puts)
    short_put = any(p.side == "SELL" for p in puts)

    if has_stock and short_call and long_put:
        return "Collar"
    if has_stock and short_call and not puts:
        return "Covered Call"
    if has_stock and long_put and not calls:
        return "Protective Put"
    if not has_stock and long_put and short_put:
        return "Bear Put Spread"
    if not has_stock and not puts:
        if len(calls) >= 2:
            return "PMCC / Call Diagonal"
        if len(calls) == 1 and long_call:
            return "LEAP"
        if len(calls) == 1 and short_call:
            return "Covered Call (existing shares)"
    if has_stock and not calls and not puts:
        return "Stock entry" if stock[0].side == "BUY" else "Stock exit"
    return "Strategy"


# Overlay-intent prefixes carried on the signal rationale (eod_reporter stores
# "PROTECTIVE_PUT: <thesis>"), mapped to display name + the structural labels
# that PROVE the intended option hedge actually filled. If the day's structural
# label is not in that proof set, the hedge leg is missing -> mismatch.
_INTENT_PROOF: dict = {
    "PROTECTIVE_PUT": ("Protective Put", {"Protective Put", "Collar"}),
    "COLLAR": ("Collar", {"Collar"}),
    "COVERED_CALL": ("Covered Call",
                     {"Covered Call", "Covered Call (existing shares)", "Collar"}),
    "BEAR_PUT_SPREAD": ("Bear Put Spread", {"Bear Put Spread"}),
    "CALL_DIAGONAL": ("PMCC / Call Diagonal", {"PMCC / Call Diagonal"}),
    "LEAP": ("LEAP", {"LEAP"}),
}


def overlay_intent_mismatch(prefix: str, label: str) -> Optional[str]:
    """Reconcile intended overlay (signal-rationale prefix) vs. the structural,
    fills-based label. Return the intended strategy's display name when the
    intent's option hedge is NOT proven by the structural label (hedge leg
    missing); else None. Pure, total, never raises."""
    entry = _INTENT_PROOF.get((prefix or "").upper())
    if entry is None:
        return None
    display, proof_labels = entry
    return None if label in proof_labels else display
