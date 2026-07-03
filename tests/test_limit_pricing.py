import pytest
from autotrader.config import RiskConfig
from autotrader.limit_pricing import capped_limit_price, tick_size


def _cfg(bps=0.0, ticks=0.0):
    return RiskConfig(trading_env="PAPER", min_confidence=0.6, max_order_notional=2000,
                      max_position_qty=100, daily_loss_limit=500, max_gross_exposure=10000,
                      allowed_symbols=frozenset({"US.AAPL"}),
                      order_cap_bps=bps, order_cap_ticks=ticks)


def test_tick_size_two_tier_ladder():
    assert tick_size(0.50) == pytest.approx(0.0001)
    assert tick_size(5.23) == pytest.approx(0.01)
    assert tick_size(200.0) == pytest.approx(0.01)


def test_buy_cap_adds_bps_above_reference():
    # 5 bps of 200 = 0.10 -> BUY limit 200.10.
    cfg = _cfg(bps=5.0)
    assert capped_limit_price("BUY", 200.0, cfg) == pytest.approx(200.10)


def test_sell_cap_subtracts_bps_below_reference():
    cfg = _cfg(bps=5.0)
    assert capped_limit_price("SELL", 200.0, cfg) == pytest.approx(199.90)


def test_tick_floor_dominates_on_low_priced_name():
    # CLOV-like $5.23, 1 bps of 5.23 = 0.0005 (< a tick), tick floor of 2 ticks = 0.02.
    cfg = _cfg(bps=1.0, ticks=2.0)
    assert capped_limit_price("BUY", 5.23, cfg) == pytest.approx(5.25)   # 5.23 + 0.02
    assert capped_limit_price("SELL", 5.23, cfg) == pytest.approx(5.21)  # 5.23 - 0.02


def test_bps_dominates_on_high_priced_name():
    # 5 bps of 200 = 0.10 > 2 ticks (0.02); bps term wins.
    cfg = _cfg(bps=5.0, ticks=2.0)
    assert capped_limit_price("BUY", 200.0, cfg) == pytest.approx(200.10)


def test_result_is_rounded_to_the_tick():
    # 3 bps of 13.60 = 0.00408 -> caps to a whole tick multiple.
    cfg = _cfg(bps=3.0, ticks=1.0)
    px = capped_limit_price("BUY", 13.60, cfg)
    assert round(px / 0.01) == pytest.approx(px / 0.01)   # exact tick multiple
    assert px >= 13.60


def test_zero_caps_returns_reference_rounded():
    cfg = _cfg(bps=0.0, ticks=0.0)
    assert capped_limit_price("BUY", 100.0, cfg) == pytest.approx(100.0)
