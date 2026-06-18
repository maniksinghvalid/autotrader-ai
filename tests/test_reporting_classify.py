from datetime import date

from autotrader.reporting.classify import Leg, classify_strategy
from autotrader.reporting.option_code import ParsedOption


def _call(strike, side="SELL"):
    return Leg(side, 2, 0.20, ParsedOption("US.X", date(2026, 8, 21), strike, "CALL"))


def _put(strike, side="BUY"):
    return Leg(side, 2, 0.16, ParsedOption("US.X", date(2026, 7, 31), strike, "PUT"))


def _stock(side="BUY"):
    return Leg(side, 200, 5.0, None)


def test_covered_call():
    assert classify_strategy([_stock(), _call(7.0)]) == "Covered Call"


def test_protective_put():
    assert classify_strategy([_stock(), _put(4.5)]) == "Protective Put"


def test_collar():
    assert classify_strategy([_stock(), _call(6.0), _put(4.5)]) == "Collar"


def test_bear_put_spread():
    assert classify_strategy([_put(5.0, "BUY"), _put(4.5, "SELL")]) == "Bear Put Spread"


def test_pmcc_two_calls():
    assert classify_strategy([_call(5.0, "BUY"), _call(7.0, "SELL")]) == "PMCC / Call Diagonal"


def test_leap_single_long_call():
    assert classify_strategy([_call(5.0, "BUY")]) == "LEAP"


def test_covered_call_existing_shares():
    assert classify_strategy([_call(47.41, "SELL")]) == "Covered Call (existing shares)"


def test_stock_entry_and_exit():
    assert classify_strategy([_stock("BUY")]) == "Stock entry"
    assert classify_strategy([_stock("SELL")]) == "Stock exit"


def test_unrecognized_is_generic():
    assert classify_strategy([]) == "Strategy"
