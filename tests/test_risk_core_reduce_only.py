from autotrader.config import RiskConfig
from autotrader.domain import AccountSnapshot, OrderRequest, Position
from autotrader.risk_core import evaluate


def _cfg(**kw):
    base = dict(trading_env="PAPER", min_confidence=0.6, max_order_notional=2000,
                max_position_qty=100, daily_loss_limit=500, max_gross_exposure=50000,
                allowed_symbols=frozenset({"US.AAPL"}), daily_loss_halt=1000)
    base.update(kw)
    return RiskConfig(**base)


def _snap(day_pnl, qty):
    pos = (Position("US.AAPL", qty, 100.0),) if qty else ()
    return AccountSnapshot(cash=0.0, total_assets=qty * 100.0, day_pnl=day_pnl,
                           stale=False, positions=pos)


def _sell(qty):
    return OrderRequest("US.AAPL", "SELL", qty, "MARKET", None, "c1")


def _buy(qty):
    return OrderRequest("US.AAPL", "BUY", qty, "MARKET", None, "c2")


def test_reduce_only_sell_passes_during_loss_breach():
    d = evaluate(_sell(40), _snap(-1200.0, 80), _cfg(), ref_price=100.0)
    assert d.approved, d.reason


def test_full_exit_passes_even_over_notional_cap():
    d = evaluate(_sell(100), _snap(-1200.0, 100), _cfg(), ref_price=100.0)
    assert d.approved, d.reason


def test_buy_still_blocked_during_loss_breach():
    d = evaluate(_buy(1), _snap(-600.0, 0), _cfg(), ref_price=100.0)
    assert not d.approved
    assert "loss" in d.reason.lower()


def test_oversized_sell_that_would_short_is_rejected():
    d = evaluate(_sell(120), _snap(0.0, 80), _cfg(max_order_notional=1e9),
                 ref_price=100.0)
    assert not d.approved
    assert "long-only" in d.reason


def test_reduce_only_still_blocked_on_stale_snapshot():
    snap = AccountSnapshot(cash=0.0, total_assets=8000.0, day_pnl=-1200.0,
                           stale=True, positions=(Position("US.AAPL", 80, 100.0),))
    d = evaluate(_sell(40), snap, _cfg(), ref_price=100.0)
    assert not d.approved
    assert "stale" in d.reason
