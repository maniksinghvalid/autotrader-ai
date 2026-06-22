from autotrader.config import RiskConfig
from autotrader.domain import AccountSnapshot, Position
from autotrader.rebalance import compute_plan, target_fractions


def _cfg(**kw):
    base = dict(
        trading_env="PAPER", min_confidence=0.6, max_order_notional=1e9,
        max_position_qty=10_000, daily_loss_limit=500, max_gross_exposure=1e9,
        allowed_symbols=frozenset({"US.AAPL", "US.MSFT"}),
        rebalance_band_pct=5.0, rebalance_min_notional=200.0,
        rebalance_cash_buffer_pct=10.0,
    )
    base.update(kw)
    return RiskConfig(**base)


def test_target_fractions_renormalize_and_buffer():
    # scores 30/10 -> 0.75/0.25 of investable; investable = 1 - 0.10 buffer
    fr = target_fractions({"US.AAPL": 30.0, "US.MSFT": 10.0},
                          allowed=frozenset({"US.AAPL", "US.MSFT"}),
                          cash_buffer_pct=10.0)
    assert abs(fr["US.AAPL"] - 0.675) < 1e-9   # 0.75 * 0.90
    assert abs(fr["US.MSFT"] - 0.225) < 1e-9   # 0.25 * 0.90


def test_within_band_is_skipped():
    snap = AccountSnapshot(cash=5500.0, total_assets=10000.0, day_pnl=0.0,
                           stale=False, positions=(Position("US.AAPL", 45, 100.0),))
    plan = compute_plan(snap, {"US.AAPL": 100.0}, {"US.AAPL": 100.0}, _cfg())
    assert plan.trades == ()
    assert ("US.AAPL", "WITHIN_BAND") in plan.skipped


def test_overweight_trims_partial_sell():
    snap = AccountSnapshot(cash=2000.0, total_assets=10000.0, day_pnl=0.0,
                           stale=False, positions=(Position("US.AAPL", 80, 100.0),))
    plan = compute_plan(snap, {"US.AAPL": 100.0}, {"US.AAPL": 100.0}, _cfg())
    assert len(plan.trades) == 1
    t = plan.trades[0]
    assert (t.symbol, t.side, t.action) == ("US.AAPL", "SELL", "TRIM")
    assert t.qty == 35 and t.new_total_qty == 45


def test_underweight_tops_up_buy():
    snap = AccountSnapshot(cash=9000.0, total_assets=10000.0, day_pnl=0.0,
                           stale=False, positions=(Position("US.AAPL", 10, 100.0),))
    plan = compute_plan(snap, {"US.AAPL": 100.0}, {"US.AAPL": 100.0}, _cfg())
    t = plan.trades[0]
    assert (t.symbol, t.side, t.action) == ("US.AAPL", "BUY", "TOPUP")
    assert t.qty == 35 and t.new_total_qty == 45


def test_min_notional_skips_churn():
    snap = AccountSnapshot(cash=4490.0, total_assets=10000.0, day_pnl=0.0,
                           stale=False, positions=(Position("US.AAPL", 55, 100.0),))
    cfg = _cfg(rebalance_min_notional=2000.0)
    plan = compute_plan(snap, {"US.AAPL": 100.0}, {"US.AAPL": 100.0}, cfg)
    assert plan.trades == ()
    assert ("US.AAPL", "SKIPPED_MIN_NOTIONAL") in plan.skipped


def test_untargeted_position_left_alone():
    snap = AccountSnapshot(cash=0.0, total_assets=10000.0, day_pnl=0.0, stale=False,
                           positions=(Position("US.NIO", 100, 50.0),))
    plan = compute_plan(snap, {"US.AAPL": 100.0}, {"US.AAPL": 100.0}, _cfg())
    assert all(t.symbol != "US.NIO" for t in plan.trades)


def test_trims_ordered_before_topups():
    snap = AccountSnapshot(cash=1000.0, total_assets=10000.0, day_pnl=0.0,
                           stale=False,
                           positions=(Position("US.AAPL", 80, 100.0),
                                      Position("US.MSFT", 10, 100.0)))
    plan = compute_plan(snap, {"US.AAPL": 100.0, "US.MSFT": 100.0},
                        {"US.AAPL": 100.0, "US.MSFT": 100.0}, _cfg())
    actions = [t.action for t in plan.trades]
    assert actions.index("TRIM") < actions.index("TOPUP")


def test_missing_price_symbol_skipped():
    snap = AccountSnapshot(cash=10000.0, total_assets=10000.0, day_pnl=0.0,
                           stale=False, positions=())
    plan = compute_plan(snap, {"US.AAPL": 100.0}, {}, _cfg())  # no price
    assert plan.trades == ()
    assert ("US.AAPL", "NO_PRICE") in plan.skipped


def test_covered_position_trims_only_to_floor():
    # 150 shares @ $100 = $15000 (overweight), 1 short call pledges 100 shares.
    # Single-symbol allow-list → target weight 0.90 → target_value 9000 → desired
    # trim 60, but coverage caps the sell at 150-100 = 50 (floor of 100 kept).
    snap = AccountSnapshot(
        cash=0.0, total_assets=10000.0, day_pnl=0.0, stale=False,
        positions=(Position("US.AAPL", 150, 100.0),
                   Position("US.AAPL260821C110000", -1, 2.0)),
    )
    cfg = _cfg(allowed_symbols=frozenset({"US.AAPL"}))
    plan = compute_plan(snap, {"US.AAPL": 100.0}, {"US.AAPL": 100.0}, cfg)
    assert len(plan.trades) == 1
    t = plan.trades[0]
    assert (t.symbol, t.side, t.action) == ("US.AAPL", "SELL", "TRIM")
    assert t.qty == 50 and t.new_total_qty == 100


def test_fully_covered_position_skipped():
    # 100 shares, 1 short call (floor 100). Overweight, but free-to-sell = 0.
    snap = AccountSnapshot(
        cash=0.0, total_assets=10000.0, day_pnl=0.0, stale=False,
        positions=(Position("US.AAPL", 100, 100.0),
                   Position("US.AAPL260821C110000", -1, 2.0)),
    )
    cfg = _cfg(allowed_symbols=frozenset({"US.AAPL"}))
    plan = compute_plan(snap, {"US.AAPL": 100.0}, {"US.AAPL": 100.0}, cfg)
    assert plan.trades == ()
    assert ("US.AAPL", "COVERED_FLOOR") in plan.skipped


def test_coverage_cap_below_min_notional_skipped():
    # 101 shares, 1 short call → free-to-sell = 1 share = $100 < $200 min notional.
    snap = AccountSnapshot(
        cash=0.0, total_assets=10000.0, day_pnl=0.0, stale=False,
        positions=(Position("US.AAPL", 101, 100.0),
                   Position("US.AAPL260821C110000", -1, 2.0)),
    )
    cfg = _cfg(allowed_symbols=frozenset({"US.AAPL"}))
    plan = compute_plan(snap, {"US.AAPL": 100.0}, {"US.AAPL": 100.0}, cfg)
    assert plan.trades == ()
    assert ("US.AAPL", "SKIPPED_MIN_NOTIONAL") in plan.skipped


def test_no_short_calls_trims_unchanged():
    # Regression: with no short call, behavior is identical to before (full trim).
    # 150 shares @ $100, single-symbol target 0.90 → desired trim 60, uncapped.
    snap = AccountSnapshot(
        cash=0.0, total_assets=10000.0, day_pnl=0.0, stale=False,
        positions=(Position("US.AAPL", 150, 100.0),),
    )
    cfg = _cfg(allowed_symbols=frozenset({"US.AAPL"}))
    plan = compute_plan(snap, {"US.AAPL": 100.0}, {"US.AAPL": 100.0}, cfg)
    assert len(plan.trades) == 1
    assert plan.trades[0].qty == 60 and plan.trades[0].new_total_qty == 90
