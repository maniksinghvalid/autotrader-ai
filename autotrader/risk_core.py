"""Deterministic risk core — the safety spine. Pure: (request, snapshot, cfg) ->
RiskDecision. No I/O, no SDK, no randomness. The strategy/LLM layer can never
reach place_order except through an approved decision here (research §4.3)."""
from __future__ import annotations

import math
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
    sym = req.symbol.upper()

    # Universal guards — apply to every order (equity and option) regardless of
    # reduce-only status, so they run before any equity-specific setup.
    # 1. Environment routing — v1 is PAPER-only.
    if cfg.trading_env != "PAPER":
        return RiskDecision(False, f"env routing: {cfg.trading_env} not allowed in paper-only v1")

    # 2. Never trade on a stale snapshot.
    if snapshot.stale:
        return RiskDecision(False, "account snapshot is stale; refusing to trade")

    # Option legs follow their own rules (coverage, contracts, premium); the
    # equity long-only / notional / exposure caps below do not apply leg-by-leg.
    # Placed before the equity reduce-only setup so that setup stays equity-only.
    if req.option is not None:
        return _evaluate_option_leg(req, snapshot, cfg, ref_price)

    # Reduce-only exit: a SELL that strictly lowers an existing long position can
    # never INCREASE risk, so the risk-increasing caps (daily-loss halt, order
    # notional, gross exposure) are skipped for it. Env / stale / allow-list /
    # long-only checks still apply. This lets the loss-halt flatten and strategy
    # stop-loss exits liquidate even while the daily-loss limit is breached.
    held = snapshot.position_qty(sym)
    resulting = held + (req.qty if req.side == "BUY" else -req.qty)
    is_reduce_only = req.side == "SELL" and 0 <= resulting < held

    # 3. Daily-loss halt — skipped for reduce-only exits.
    if not is_reduce_only and snapshot.day_pnl <= -abs(cfg.daily_loss_limit):
        return RiskDecision(False, f"daily loss limit breached: pnl={snapshot.day_pnl}")

    # 4. Symbol allow-list.
    if sym not in cfg.allowed_symbols:
        return RiskDecision(False, f"symbol {req.symbol} not in allow-list")

    # 5. A reference price must exist to size/clamp the order.
    eff_price = req.limit_price if (req.order_type == "LIMIT" and req.limit_price) else ref_price
    if eff_price is None or not math.isfinite(eff_price) or eff_price <= 0:
        return RiskDecision(False, "no usable reference price for risk sizing")

    # 6. Max order notional clamp — skipped for reduce-only exits.
    notional = _notional(req, eff_price)
    if not is_reduce_only and (not math.isfinite(notional) or notional > cfg.max_order_notional):
        return RiskDecision(False, f"order notional {notional:.2f} > cap {cfg.max_order_notional}")

    # 7. Resulting position cap. v1 is long-only: a SELL may at most flatten the
    #    held position (resulting >= 0); a negative result is an opening short.
    if resulting < 0:
        return RiskDecision(False, f"long-only: SELL would short position to {resulting}")
    if abs(resulting) > cfg.max_position_qty:
        return RiskDecision(False, f"resulting position {resulting} > cap {cfg.max_position_qty}")

    # 8. Gross exposure cap after this order — skipped for reduce-only exits.
    projected = snapshot.gross_exposure() + notional
    if not is_reduce_only and projected > cfg.max_gross_exposure:
        return RiskDecision(False, f"gross exposure {projected:.2f} > cap {cfg.max_gross_exposure}")

    return RiskDecision(True, "OK")


def _evaluate_option_leg(req: OrderRequest, snapshot: AccountSnapshot,
                         cfg: RiskConfig, ref_price: Optional[float]) -> RiskDecision:
    """O1 option gate. Env + stale already checked by the caller.
    - underlying must be allow-listed
    - contracts <= max_option_contracts (cap 0 => options off)
    - a short OPEN leg must be share-covered (covered call) — never naked
    - a long (debit) OPEN leg's premium outlay is capped per trade
    Gross-exposure aggregation and the daily premium cap are later phases."""
    opt = req.option
    underlying = opt.underlying.upper()
    if underlying not in cfg.allowed_symbols:
        return RiskDecision(False, f"underlying {opt.underlying} not in allow-list")

    premium = req.limit_price if (req.order_type == "LIMIT" and req.limit_price) else ref_price
    if premium is None or not math.isfinite(premium) or premium <= 0:
        return RiskDecision(False, "no usable option premium for risk sizing")

    if req.qty > cfg.max_option_contracts:
        return RiskDecision(False,
                            f"contracts {req.qty} > cap {cfg.max_option_contracts}")

    if req.side == "SELL" and req.position_effect == "OPEN":
        need = req.qty * opt.multiplier
        held = snapshot.position_qty(underlying)
        if held < need:
            return RiskDecision(False,
                                f"uncovered short: held {held} < required {need} shares")
    else:
        # O1 only OPENs (covered-call SELL OPEN, protective-put BUY OPEN). This debit-cost
        # cap assumes a debit. CLOSE legs (buy-to-close / sell-to-close credits) need
        # explicit handling in O4 — do not assume this formula is correct for CLOSE.
        cost = req.qty * premium * opt.multiplier
        if not math.isfinite(cost) or cost > cfg.max_option_premium_per_trade:
            return RiskDecision(False,
                                f"option premium {cost:.2f} > cap "
                                f"{cfg.max_option_premium_per_trade}")

    return RiskDecision(True, "OK")
