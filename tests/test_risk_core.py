import pytest
from autotrader.domain import OrderRequest, AccountSnapshot, Position
from autotrader.config import RiskConfig
from autotrader.risk_core import evaluate, RiskDecision


def _cfg(**over):
    base = dict(trading_env="PAPER", min_confidence=0.6, max_order_notional=2000,
                max_position_qty=200, daily_loss_limit=500, max_gross_exposure=10000,
                allowed_symbols=frozenset({"US.AAPL"}))
    base.update(over)
    return RiskConfig(**base)


def _snap(**over):
    base = dict(cash=10000.0, total_assets=10000.0, day_pnl=0.0, stale=False, positions=())
    base.update(over)
    return AccountSnapshot(**base)


def _req(**over):
    base = dict(symbol="US.AAPL", side="BUY", qty=5, order_type="LIMIT",
                limit_price=100.0, client_order_id="cid")
    base.update(over)
    return OrderRequest(**base)


def test_clean_order_is_approved():
    d = evaluate(_req(), _snap(), _cfg(), ref_price=100.0)
    assert isinstance(d, RiskDecision)
    assert d.approved is True
    assert d.reason == "OK"


def test_reject_on_stale_snapshot():
    d = evaluate(_req(), _snap(stale=True), _cfg(), ref_price=100.0)
    assert d.approved is False
    assert "stale" in d.reason.lower()


def test_reject_symbol_not_in_allow_list():
    d = evaluate(_req(symbol="US.TSLA"), _snap(), _cfg(), ref_price=100.0)
    assert d.approved is False
    assert "allow" in d.reason.lower()


def test_reject_when_notional_exceeds_cap():
    d = evaluate(_req(qty=50), _snap(), _cfg(max_order_notional=2000), ref_price=100.0)
    assert d.approved is False
    assert "notional" in d.reason.lower()


def test_reject_when_resulting_position_exceeds_qty_cap():
    snap = _snap(positions=(Position("US.AAPL", qty=98, avg_price=100.0),))
    d = evaluate(_req(qty=5), snap, _cfg(max_position_qty=100), ref_price=100.0)
    assert d.approved is False
    assert "position" in d.reason.lower()


def test_reject_when_daily_loss_limit_breached():
    d = evaluate(_req(), _snap(day_pnl=-600.0), _cfg(daily_loss_limit=500), ref_price=100.0)
    assert d.approved is False
    assert "daily loss" in d.reason.lower()


def test_reject_when_gross_exposure_cap_breached():
    snap = _snap(positions=(Position("US.AAPL", qty=95, avg_price=100.0),))
    d = evaluate(_req(qty=10), snap, _cfg(max_gross_exposure=10000), ref_price=100.0)
    assert d.approved is False
    assert "exposure" in d.reason.lower()


def test_reject_live_env_routing_in_paper_only_v1():
    d = evaluate(_req(), _snap(), _cfg(trading_env="LIVE"), ref_price=100.0)
    assert d.approved is False
    assert "live" in d.reason.lower()


def test_reject_missing_ref_price():
    d = evaluate(_req(order_type="MARKET", limit_price=None), _snap(), _cfg(), ref_price=None)
    assert d.approved is False
    assert "price" in d.reason.lower()


def test_mixed_case_symbol_uses_uppercase_position_for_cap():
    # Allow-list and position lookup must agree on case: a lowercase symbol must
    # not pass the allow-list while missing the stored uppercase position (which
    # would compute the cap off a base of 0 and bypass it).
    snap = _snap(positions=(Position("US.AAPL", qty=98, avg_price=100.0),))
    d = evaluate(_req(symbol="us.aapl", qty=5), snap, _cfg(max_position_qty=100), ref_price=100.0)
    assert d.approved is False
    assert "position" in d.reason.lower()


def test_reject_sell_that_would_open_a_short():
    # v1 is long-only: selling more than held must be rejected, not allowed to
    # build a short up to the cap via abs().
    snap = _snap(positions=(Position("US.AAPL", qty=3, avg_price=100.0),))
    d = evaluate(_req(side="SELL", qty=10), snap, _cfg(), ref_price=100.0)
    assert d.approved is False
    assert "short" in d.reason.lower()


def test_sell_to_flat_is_allowed():
    # Selling exactly the held quantity (resulting == 0) must NOT be rejected.
    snap = _snap(positions=(Position("US.AAPL", qty=5, avg_price=100.0),))
    d = evaluate(_req(side="SELL", qty=5), snap, _cfg(), ref_price=100.0)
    assert d.approved is True


def test_reject_nan_reference_price():
    # A non-finite price is truthy and slips past `<= 0`; it must be rejected so
    # notional/exposure caps cannot be bypassed by a NaN.
    d = evaluate(_req(order_type="MARKET", limit_price=None), _snap(), _cfg(),
                 ref_price=float("nan"))
    assert d.approved is False
    assert "price" in d.reason.lower()
