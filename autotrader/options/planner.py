"""Expand one overlay Signal into concrete leg OrderRequests, or a structured
skip. Pure given a chain provider (broker.get_option_chain): no order placement,
no SDK. Contract = delta+DTE selection (D5); short overlays require >=100 held
shares (D3). Imports domain, config, chain, overlays, router (cid helper)."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Optional, Tuple, Union

from autotrader.config import RiskConfig
from autotrader.domain import AccountSnapshot, OrderRequest, OverlayType, Signal
from autotrader.options.chain import OptionQuote, select_contract, to_contract
from autotrader.options.overlays import ExitRule, REGISTRY
from autotrader.router import OrderRouter


@dataclass(frozen=True)
class OverlayLeg:
    request: OrderRequest
    quote: OptionQuote


@dataclass(frozen=True)
class OverlayPlan:
    overlay: OverlayType
    underlying: str
    legs: Tuple[OverlayLeg, ...]
    exit: ExitRule
    correlation_id: str


@dataclass(frozen=True)
class OverlaySkip:
    overlay: OverlayType
    underlying: str
    reason: str   # SKIP_OVERLAY_DISABLED | SKIP_UNSUPPORTED_OVERLAY
                  # | SKIP_NO_UNDERLYING | SKIP_NO_CONTRACT | SKIP_INVALID_STRUCTURE


def _validate_structure(overlay: OverlayType, legs: Tuple["OverlayLeg", ...]) -> Optional[str]:
    """Defined-risk sanity check after contracts are selected. Returns a skip
    reason string or None. Defends the coverage guarantee the risk core relies on.
    COLLAR and LEAP are intentionally not validated here: the collar's short call
    is share-covered and validated by the risk core; LEAP has no short leg."""
    longs = [l for l in legs if l.request.side == "BUY"]
    shorts = [l for l in legs if l.request.side == "SELL"]
    if overlay is OverlayType.BEAR_PUT_SPREAD:
        lo, sh = longs[0].quote, shorts[0].quote
        if lo.expiry != sh.expiry:
            return "SKIP_INVALID_STRUCTURE"
        if not (lo.strike > sh.strike):
            return "SKIP_INVALID_STRUCTURE"
        if (lo.premium - sh.premium) <= 0:   # must be a net debit
            return "SKIP_INVALID_STRUCTURE"
    elif overlay is OverlayType.CALL_DIAGONAL:
        lo, sh = longs[0].quote, shorts[0].quote
        if not (lo.strike <= sh.strike):     # long is deeper ITM / not higher strike
            return "SKIP_INVALID_STRUCTURE"
        if not (lo.expiry > sh.expiry):      # long dated later than the short
            return "SKIP_INVALID_STRUCTURE"
    return None


def build_overlay_plan(signal: Signal, snapshot: AccountSnapshot, chain_provider,
                       cfg: RiskConfig, signal_id: str,
                       asof: date) -> Union[OverlayPlan, OverlaySkip]:
    overlay = signal.overlay
    if overlay is None:
        raise ValueError("build_overlay_plan requires signal.overlay to be set")
    underlying = signal.symbol.upper()

    if overlay.value not in cfg.allowed_overlays:
        return OverlaySkip(overlay, underlying, "SKIP_OVERLAY_DISABLED")
    if overlay not in REGISTRY:
        return OverlaySkip(overlay, underlying, "SKIP_UNSUPPORTED_OVERLAY")

    deff = REGISTRY[overlay]

    # Sizing fork: share-covered overlays size off held shares; non-covered
    # strategies (spread/diagonal/LEAP) size off the configured default count.
    if deff.requires_underlying:
        held = snapshot.position_qty(underlying)
        contracts = min(held // 100, cfg.max_option_contracts)
        if contracts < 1:
            return OverlaySkip(overlay, underlying, "SKIP_NO_UNDERLYING")
    else:
        contracts = min(cfg.option_default_contracts, cfg.max_option_contracts)
        if contracts < 1:
            return OverlaySkip(overlay, underlying, "SKIP_OVERLAY_DISABLED")

    corr = f"ov-{signal_id}-{overlay.value}"
    legs = []
    anchor_expiry = None
    for i, spec in enumerate(deff.legs):
        target_delta = spec.target_delta if spec.target_delta is not None else cfg.option_target_delta
        dte_min = spec.dte_min if spec.dte_min is not None else cfg.option_dte_min
        dte_max = spec.dte_max if spec.dte_max is not None else cfg.option_dte_max
        pin = anchor_expiry if (deff.single_expiry and i > 0) else None
        quotes = chain_provider.get_option_chain(underlying, spec.right)
        q = select_contract(quotes, spec.right, target_delta, dte_min, dte_max,
                            asof, pin_expiry=pin)
        if q is None:
            return OverlaySkip(overlay, underlying, "SKIP_NO_CONTRACT")
        if i == 0:
            anchor_expiry = q.expiry
        contract = to_contract(q)
        qty = contracts * spec.ratio
        cid = OrderRouter.make_client_order_id(
            q.code, spec.side, qty, f"{signal_id}-{overlay.value}-{i}")
        req = OrderRequest(symbol=q.code, side=spec.side, qty=qty,
                           order_type="MARKET", limit_price=None,
                           client_order_id=cid, option=contract,
                           position_effect=spec.position_effect,
                           correlation_id=corr)
        legs.append(OverlayLeg(req, q))

    bad = _validate_structure(overlay, legs)
    if bad is not None:
        return OverlaySkip(overlay, underlying, bad)

    exit_rule = ExitRule(dte_to_close=cfg.option_dte_to_close,
                         profit_target_pct=cfg.option_profit_target_pct)
    return OverlayPlan(overlay, underlying, tuple(legs), exit_rule, corr)
