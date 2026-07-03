"""Strategy-book segregation primitives (pure; no SDK, no broker handle).

A "claim" marks a symbol as belonging to the internal BREAKOUT book. Absence of a
claim means the symbol belongs to the AI/rebalance book (spec D4). These helpers
operate on a plain {symbol: origin} mapping supplied by the caller — persistence
lives in db.py, enforcement lives in main.py. See
docs/superpowers/specs/2026-07-03-strategy-book-segregation-design.md.
"""
from __future__ import annotations

ORIGIN_BREAKOUT = "BREAKOUT"

# Skip / result reasons surfaced by the TradeEngine choke points.
SKIP_BOOK_CONFLICT = "BOOK_CONFLICT"          # external signal vs. breakout-owned symbol
SKIP_BOOK_CLAIMED = "BOOK_CLAIMED"            # rebalance excludes a claimed symbol
SKIP_POSITION_NOT_OWNED = "POSITION_NOT_OWNED"  # breakout tick won't manage an unclaimed position


def is_breakout_claimed(symbol: str, claims: dict) -> bool:
    """True iff `symbol` is currently claimed by the BREAKOUT book."""
    return claims.get(symbol) == ORIGIN_BREAKOUT
