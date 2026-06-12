"""Deterministic risk core — the safety spine. Pure: (request, snapshot, cfg) ->
RiskDecision. No I/O, no SDK, no randomness. The strategy/LLM layer can never
reach place_order except through an approved decision here (research §4.3)."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from autotrader.config import RiskConfig
from autotrader.domain import AccountSnapshot, OrderRequest


@dataclass(frozen=True)
class RiskDecision:
    approved: bool
    reason: str


def _notional(req: OrderRequest, ref_price: float) -> float:
    price = req.limit_price if req.order_type == "LIMIT" and req.limit_price else ref_price
    return abs(req.qty) * price


def evaluate(req: OrderRequest, snapshot: AccountSnapshot, cfg: RiskConfig,
             ref_price: Optional[float]) -> RiskDecision:
    # 1. Environment routing — v1 is PAPER-only. LIVE is gated by a later phase
    #    AND a manual GUI unlock; never auto-promote (research R4/E17).
    if cfg.trading_env != "PAPER":
        return RiskDecision(False, f"env routing: {cfg.trading_env} not allowed in paper-only v1")

    # 2. Never trade on a stale snapshot (research R7/E1).
    if snapshot.stale:
        return RiskDecision(False, "account snapshot is stale; refusing to trade")

    # 3. Daily-loss halt (research §4.3). day_pnl is negative when losing.
    if snapshot.day_pnl <= -abs(cfg.daily_loss_limit):
        return RiskDecision(False, f"daily loss limit breached: pnl={snapshot.day_pnl}")

    # 4. Symbol allow-list.
    if req.symbol.upper() not in cfg.allowed_symbols:
        return RiskDecision(False, f"symbol {req.symbol} not in allow-list")

    # 5. A reference price must exist to size/clamp the order.
    eff_price = req.limit_price if (req.order_type == "LIMIT" and req.limit_price) else ref_price
    if not eff_price or eff_price <= 0:
        return RiskDecision(False, "no usable reference price for risk sizing")

    # 6. Max order notional clamp (research R3).
    notional = _notional(req, eff_price)
    if notional > cfg.max_order_notional:
        return RiskDecision(False, f"order notional {notional:.2f} > cap {cfg.max_order_notional}")

    # 7. Resulting position cap (only adds for BUY in v1 long-only strategy).
    resulting = snapshot.position_qty(req.symbol) + (req.qty if req.side == "BUY" else -req.qty)
    if abs(resulting) > cfg.max_position_qty:
        return RiskDecision(False, f"resulting position {resulting} > cap {cfg.max_position_qty}")

    # 8. Gross exposure cap after this order.
    projected = snapshot.gross_exposure() + notional
    if projected > cfg.max_gross_exposure:
        return RiskDecision(False, f"gross exposure {projected:.2f} > cap {cfg.max_gross_exposure}")

    return RiskDecision(True, "OK")
