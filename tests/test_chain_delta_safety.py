"""C4: degraded greeks must SKIP, never select. An all-zero-delta chain makes
every candidate 'equidistant' and the tie-break sells the deepest ITM call."""
import math
from datetime import date

from autotrader.options.chain import OptionQuote, select_contract


def _q(strike, delta, premium=1.0, expiry=date(2026, 8, 7)):
    return OptionQuote(code=f"TESTC{int(strike)}", underlying="US.TEST",
                       expiry=expiry, strike=strike, right="CALL",
                       delta=delta, premium=premium)


ASOF = date(2026, 7, 6)


def test_all_zero_delta_chain_selects_nothing():
    chain = [_q(50, 0.0), _q(100, 0.0), _q(150, 0.0)]
    assert select_contract(chain, "CALL", 0.30, 20, 60, ASOF) is None


def test_nan_delta_row_never_wins_regardless_of_order():
    good = _q(105, 0.30)
    nan = _q(50, float("nan"))
    for chain in ([nan, good], [good, nan]):
        pick = select_contract(chain, "CALL", 0.30, 20, 60, ASOF)
        assert pick is good


def test_valid_chain_still_selects_closest_delta():
    chain = [_q(95, 0.55), _q(105, 0.31), _q(115, 0.18)]
    assert select_contract(chain, "CALL", 0.30, 20, 60, ASOF).strike == 105
