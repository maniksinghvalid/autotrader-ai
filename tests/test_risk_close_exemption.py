"""Spec W6: BUY-to-CLOSE / SELL-to-CLOSE option legs must never be blocked by
the ENTRY premium budget — mirroring the equity reduce-only exemption, the
risk system must never block its own exit (review Important #5)."""
from datetime import date

from autotrader.config import RiskConfig
from autotrader.domain import AccountSnapshot, OptionContract, OrderRequest
from autotrader.risk_core import evaluate


def _cfg():
    return RiskConfig(trading_env="PAPER", min_confidence=0.6,
                      max_order_notional=20000, max_position_qty=100,
                      daily_loss_limit=500, max_gross_exposure=100000,
                      allowed_symbols=frozenset({"US.AAPL"}),
                      max_option_contracts=5, option_max_risk_pct=0.02)


def _snap(stale=False):
    # NLV 10_000 -> premium budget = 200
    return AccountSnapshot(cash=10000.0, total_assets=10000.0, day_pnl=0.0,
                           stale=stale)


def _leg(side, effect, premium=5.0):
    opt = OptionContract(underlying="US.AAPL", expiry=date(2026, 9, 18),
                         strike=110.0, right="CALL",
                         code="US.AAPL260918C110000")
    return OrderRequest(symbol=opt.code, side=side, qty=1, order_type="LIMIT",
                        limit_price=premium, client_order_id="t",
                        option=opt, position_effect=effect)


def test_buy_to_close_above_budget_is_approved():
    # 1 contract x $5 x 100 = $500 premium > $200 budget — but it is an EXIT.
    d = evaluate(_leg("BUY", "CLOSE"), _snap(), _cfg(), ref_price=5.0)
    assert d.approved, d.reason


def test_sell_to_close_above_budget_is_approved():
    d = evaluate(_leg("SELL", "CLOSE"), _snap(), _cfg(), ref_price=5.0)
    assert d.approved, d.reason


def test_buy_to_open_above_budget_still_vetoed():
    d = evaluate(_leg("BUY", "OPEN"), _snap(), _cfg(), ref_price=5.0)
    assert not d.approved and "budget" in d.reason


def test_close_leg_still_refused_on_stale_snapshot():
    d = evaluate(_leg("BUY", "CLOSE"), _snap(stale=True), _cfg(), ref_price=5.0)
    assert not d.approved and "stale" in d.reason
