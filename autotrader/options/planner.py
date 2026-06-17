"""Expand one overlay Signal into concrete leg OrderRequests, or a structured
skip. Pure given a chain provider (broker.get_option_chain): no order placement,
no SDK. Contract = delta+DTE selection (D5); short overlays require >=100 held
shares (D3). Imports domain, config, chain, overlays, router (cid helper)."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Tuple, Union

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
                  # | SKIP_NO_UNDERLYING | SKIP_NO_CONTRACT


def build_overlay_plan(signal: Signal, snapshot: AccountSnapshot, chain_provider,
                       cfg: RiskConfig, signal_id: str,
                       asof: date) -> Union[OverlayPlan, OverlaySkip]:
    overlay = signal.overlay
    underlying = signal.symbol.upper()

    if overlay.value not in cfg.allowed_overlays:
        return OverlaySkip(overlay, underlying, "SKIP_OVERLAY_DISABLED")
    if overlay not in REGISTRY:
        return OverlaySkip(overlay, underlying, "SKIP_UNSUPPORTED_OVERLAY")

    deff = REGISTRY[overlay]
    held = snapshot.position_qty(underlying)
    contracts = min(held // 100, cfg.max_option_contracts)
    if deff.requires_underlying and contracts < 1:
        return OverlaySkip(overlay, underlying, "SKIP_NO_UNDERLYING")

    corr = f"ov-{signal_id}-{overlay.value}"
    legs = []
    for i, spec in enumerate(deff.legs):
        quotes = chain_provider.get_option_chain(underlying, spec.right)
        q = select_contract(quotes, spec.right, cfg.option_target_delta,
                            cfg.option_dte_min, cfg.option_dte_max, asof)
        if q is None:
            return OverlaySkip(overlay, underlying, "SKIP_NO_CONTRACT")
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

    exit_rule = ExitRule(dte_to_close=cfg.option_dte_to_close,
                         profit_target_pct=cfg.option_profit_target_pct)
    return OverlayPlan(overlay, underlying, tuple(legs), exit_rule, corr)
