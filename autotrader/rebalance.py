"""Pure portfolio rebalancer. No broker/SDK/clock: (snapshot, scores, prices,
cfg) -> RebalancePlan. The engine executes the plan through the audited risk
path. Two-sided: overweight positions trim (partial SELL), underweight ones top
up (BUY); trims are ordered first so cash frees up before top-ups. Positions
with no target are left untouched (managed by their strategy/trailing stop)."""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Dict, FrozenSet, Tuple

from autotrader.config import RiskConfig
from autotrader.domain import AccountSnapshot

SHARES_PER_CONTRACT = 100   # US equity option multiplier (domain default)


@dataclass(frozen=True)
class RebalanceTrade:
    symbol: str
    side: str          # "BUY" | "SELL"
    qty: int
    action: str        # "TRIM" | "TOPUP"
    new_total_qty: int  # intended post-trade position qty (for stop consolidation)


@dataclass(frozen=True)
class RebalancePlan:
    trades: Tuple[RebalanceTrade, ...]
    skipped: Tuple[Tuple[str, str], ...]   # (symbol, reason) for logging


def target_fractions(scores: Dict[str, float], allowed: FrozenSet[str],
                     cash_buffer_pct: float) -> Dict[str, float]:
    """Renormalize positive scores (restricted to the allow-list) to fractions of
    INVESTABLE equity, where investable = 1 - cash_buffer. Unscored allowed
    symbols are treated as having zero weight; their share of investable is not
    awarded to the scored symbols, so a single symbol among N allowed receives
    at most investable/N rather than the full investable pool.  Returns {} if no
    positive score remains."""
    n_allowed = len(allowed)
    if n_allowed == 0:
        return {}
    pos = {s.upper(): v for s, v in scores.items()
           if s.upper() in allowed and v > 0}
    if not pos:
        return {}
    total = sum(pos.values())
    investable = max(0.0, 1.0 - cash_buffer_pct / 100.0)
    # Scale the investable pool by the fraction of allowed symbols that are
    # actually scored; this prevents one scored symbol from claiming the entire
    # investable budget when other allowed symbols simply have no current signal.
    investable_for_scored = investable * (len(pos) / n_allowed)
    return {s: (v / total) * investable_for_scored for s, v in pos.items()}


def compute_plan(snapshot: AccountSnapshot, scores: Dict[str, float],
                 prices: Dict[str, float], cfg: RiskConfig) -> RebalancePlan:
    fractions = target_fractions(scores, cfg.allowed_symbols,
                                 cfg.rebalance_cash_buffer_pct)
    total = snapshot.total_assets
    band = cfg.rebalance_band_pct / 100.0
    trims: list = []
    topups: list = []
    skipped: list = []
    if total <= 0:
        return RebalancePlan((), tuple((s, "NO_EQUITY") for s in fractions))

    for symbol in sorted(fractions):
        target_weight = fractions[symbol]
        price = prices.get(symbol)
        if price is None or not math.isfinite(price) or price <= 0:
            skipped.append((symbol, "NO_PRICE"))
            continue
        current_qty = snapshot.position_qty(symbol)
        current_value = current_qty * price
        current_weight = current_value / total
        drift = current_weight - target_weight
        if abs(drift) <= band:
            skipped.append((symbol, "WITHIN_BAND"))
            continue
        target_value = target_weight * total
        if drift > band:  # overweight -> trim
            qty = int((current_value - target_value) // price)
            qty = min(qty, current_qty)
            if qty <= 0:
                skipped.append((symbol, "WITHIN_BAND"))
                continue
            # Reserve shares pledged to open short calls (covered call / collar):
            # never trim below the covered floor, or the short call goes naked.
            covered_floor = (snapshot.short_option_contracts(symbol, "CALL")
                             * SHARES_PER_CONTRACT)
            qty = min(qty, max(0, current_qty - covered_floor))
            if qty <= 0:
                skipped.append((symbol, "COVERED_FLOOR"))
                continue
            if qty * price < cfg.rebalance_min_notional:
                skipped.append((symbol, "SKIPPED_MIN_NOTIONAL"))
                continue
            trims.append(RebalanceTrade(symbol, "SELL", qty, "TRIM",
                                        current_qty - qty))
        else:              # underweight -> top up
            qty = int((target_value - current_value) // price)
            if qty <= 0:
                skipped.append((symbol, "WITHIN_BAND"))
                continue
            if qty * price < cfg.rebalance_min_notional:
                skipped.append((symbol, "SKIPPED_MIN_NOTIONAL"))
                continue
            topups.append(RebalanceTrade(symbol, "BUY", qty, "TOPUP",
                                         current_qty + qty))

    return RebalancePlan(tuple(trims) + tuple(topups), tuple(skipped))
