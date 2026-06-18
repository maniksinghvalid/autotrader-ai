from datetime import date

from autotrader.reporting.classify import Leg
from autotrader.reporting.economics import StrategyEconomics, compute_economics
from autotrader.reporting.option_code import ParsedOption


def _opt(strike, right):
    return ParsedOption("US.X", date(2026, 8, 21), strike, right)


def test_covered_call_credit_and_cap():
    legs = [Leg("BUY", 200, 4.99, None),
            Leg("SELL", 2, 0.20, _opt(7.0, "CALL"))]
    e = compute_economics(legs, basis=4.99)
    assert abs(e.net_premium - 40.0) < 1e-6        # +2*0.20*100
    assert e.cap == 7.0
    assert abs(e.cap_pct - ((7.0 - 4.99) / 4.99 * 100)) < 1e-6
    assert e.floor is None and e.hedge_cost_pct is None


def test_protective_put_debit_floor_hedgecost():
    legs = [Leg("BUY", 100, 3.33, None),
            Leg("BUY", 1, 0.48, _opt(3.0, "PUT"))]
    e = compute_economics(legs, basis=3.33)
    assert abs(e.net_premium - (-48.0)) < 1e-6     # -1*0.48*100
    assert e.floor == 3.0
    assert abs(e.floor_pct - ((3.0 - 3.33) / 3.33 * 100)) < 1e-6
    assert abs(e.hedge_cost_pct - (0.48 / 3.33 * 100)) < 1e-6


def test_collar_net_and_band():
    legs = [Leg("BUY", 200, 5.03, None),
            Leg("SELL", 2, 0.14, _opt(6.0, "CALL")),
            Leg("BUY", 2, 0.16, _opt(4.5, "PUT"))]
    e = compute_economics(legs, basis=5.03)
    assert abs(e.net_premium - (-4.0)) < 1e-6      # +28 -32
    assert e.floor == 4.5 and e.cap == 6.0


def test_missing_basis_omits_pcts():
    legs = [Leg("SELL", 2, 0.20, _opt(7.0, "CALL"))]
    e = compute_economics(legs, basis=None)
    assert e.cap == 7.0
    assert e.cap_pct is None and e.floor_pct is None and e.hedge_cost_pct is None
    assert abs(e.net_premium - 40.0) < 1e-6


def test_hedge_cost_pct_uses_floor_matching_put():
    """hedge_cost_pct must use the LOWEST-strike long put (floor), not long_puts[0].

    First-listed put has strike=5.0 and price=0.90 (higher strike, higher-priced).
    Second-listed put has strike=3.0 and price=0.25 (floor — lowest strike).
    Under the old long_puts[0] bug, hedge_cost_pct would be 0.90/basis*100.
    The correct value is 0.25/basis*100.
    """
    basis = 5.00
    legs = [
        Leg("BUY", 1, 0.90, _opt(5.0, "PUT")),   # non-floor put — first-listed
        Leg("BUY", 1, 0.25, _opt(3.0, "PUT")),   # floor put — lower strike
    ]
    e = compute_economics(legs, basis=basis)
    assert e.floor == 3.0
    assert abs(e.hedge_cost_pct - (0.25 / basis * 100)) < 1e-9, (
        f"hedge_cost_pct {e.hedge_cost_pct} should be {0.25/basis*100} (floor put), "
        f"not {0.90/basis*100} (non-floor put)"
    )
