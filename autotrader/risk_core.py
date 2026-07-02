"""Deterministic risk core — the safety spine. Pure: (request, snapshot, cfg) ->
RiskDecision. No I/O, no SDK, no randomness. The strategy/LLM layer can never
reach place_order except through an approved decision here (research §4.3)."""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Optional, Tuple

from autotrader.config import RiskConfig
from autotrader.domain import AccountSnapshot, OrderRequest

# Credit-leg sizing assumes a 200% stop-loss (buy-to-close at 3x entry) => max loss
# is this multiple of the premium collected. The stop itself is enforced in O4.
_CREDIT_STOP_LOSS_MULTIPLE = 2.0


def _short_option_contracts(snapshot, underlying: str, right: str) -> int:
    """Open short contracts on `underlying` for the given right. Delegates to
    AccountSnapshot.short_option_contracts — the single source of truth."""
    return snapshot.short_option_contracts(underlying, right)


def _long_cover_contracts(coverage_legs, opt) -> int:
    """Contracts of long OPEN option legs (in the same plan) that bound the risk
    of a short leg of `opt`'s right on the same underlying (defined-risk coverage):
      short CALL  <- long CALL with strike <= short strike AND expiry >= short expiry
      short PUT   <- long PUT  with strike >= short strike AND expiry >= short expiry
    """
    total = 0
    for c in coverage_legs:
        co = c.option
        if co is None or c.side != "BUY" or c.position_effect != "OPEN":
            continue
        if co.underlying.upper() != opt.underlying.upper() or co.right != opt.right:
            continue
        if opt.right == "CALL":
            ok = co.strike <= opt.strike and co.expiry >= opt.expiry
        else:  # PUT
            ok = co.strike >= opt.strike and co.expiry >= opt.expiry
        if ok:
            total += c.qty
    return total


@dataclass(frozen=True)
class RiskDecision:
    approved: bool
    reason: str


def _notional(req: OrderRequest, ref_price: float) -> float:
    price = req.limit_price if req.order_type == "LIMIT" and req.limit_price else ref_price
    return abs(req.qty) * price


def evaluate(req: OrderRequest, snapshot: AccountSnapshot, cfg: RiskConfig,
             ref_price: Optional[float],
             coverage_legs: Tuple[OrderRequest, ...] = ()) -> RiskDecision:
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
        return _evaluate_option_leg(req, snapshot, cfg, ref_price, coverage_legs)

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
                         cfg: RiskConfig, ref_price: Optional[float],
                         coverage_legs: Tuple[OrderRequest, ...] = ()) -> RiskDecision:
    """O1 option gate. Env + stale already checked by the caller.
    - underlying must be allow-listed
    - contracts <= max_option_contracts (cap 0 => options off)
    - a short OPEN leg must be covered — by shares (CALLs only) or by a long option
      leg in the same plan (`coverage_legs`, defined-risk) — never naked
    - premium is capped by an NLV-derived budget (option_max_risk_pct): debit legs cap
      paid premium, credit legs cap 2x collected (200% stop). max_option_premium_per_trade
      is an optional absolute ceiling (0 = off); the tighter of the two binds.
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

    nlv = snapshot.total_assets
    budget = nlv * cfg.option_max_risk_pct          # max acceptable loss for this leg
    abs_ceiling = cfg.max_option_premium_per_trade  # optional absolute $ ceiling; 0 = off
    gross = req.qty * premium * opt.multiplier      # premium dollars (paid or collected)

    if req.position_effect == "CLOSE":
        # Exit legs (buy-to-close / sell-to-close) are NEVER premium-budget
        # gated — the mirror of the equity reduce-only exemption: closing an
        # option position cannot increase the premium at risk, and the risk
        # system must never block its own exit (spec W6). Env / stale /
        # allow-list / premium-sanity / contract-cap checks above still apply,
        # and the daily-loss guard below is OPEN-only already.
        return RiskDecision(True, "OK")

    if req.side == "SELL":
        # Defined-risk coverage: shares (CALLs only) or a long leg in the same plan.
        # Shares never cover a short PUT — only a long put bounds it.
        need = req.qty  # contracts
        existing_short = _short_option_contracts(snapshot, underlying, opt.right)
        share_cover = (snapshot.position_qty(underlying) // opt.multiplier
                       if opt.right == "CALL" else 0)
        long_cover = _long_cover_contracts(coverage_legs, opt)
        available = share_cover + long_cover - existing_short
        if available < need:
            return RiskDecision(
                False,
                f"uncovered short: covered {available} < required {need} contract(s) "
                f"(shares cover {share_cover}, long-leg cover {long_cover}, "
                f"{existing_short} short {opt.right} contract(s) open)")
        # Credit-leg premium cap: with the 200% stop (buy-to-close at 3x entry) the max
        # loss is 2x premium collected. Entry-discipline; the stop is enforced in O4.
        if _CREDIT_STOP_LOSS_MULTIPLE * gross > budget:
            return RiskDecision(
                False,
                f"short option premium risk {_CREDIT_STOP_LOSS_MULTIPLE * gross:.2f} "
                f"(2x collected {gross:.2f}) "
                f"> budget {budget:.2f} (NLV {nlv:.2f} x {cfg.option_max_risk_pct})")
        if abs_ceiling > 0 and gross > abs_ceiling:
            return RiskDecision(False,
                                f"option premium {gross:.2f} > absolute cap {abs_ceiling}")
    else:
        # Debit (long) OPEN: max loss = 100% of premium paid.
        if not math.isfinite(gross) or gross > budget:
            return RiskDecision(
                False,
                f"option premium debit {gross:.2f} > budget {budget:.2f} "
                f"(NLV {nlv:.2f} x {cfg.option_max_risk_pct})")
        if abs_ceiling > 0 and gross > abs_ceiling:
            return RiskDecision(False,
                                f"option premium {gross:.2f} > absolute cap {abs_ceiling}")

    # Daily-loss guard for risk-increasing OPEN legs: mirrors the equity BUY-entry
    # halt so a covered-call or protective-put OPEN is blocked when the day is
    # already in a loss-limit breach. CLOSE legs are exits and are never gated here.
    if req.position_effect == "OPEN" and snapshot.day_pnl <= -abs(cfg.daily_loss_limit):
        return RiskDecision(False, f"daily loss limit breached: pnl={snapshot.day_pnl}")

    return RiskDecision(True, "OK")
