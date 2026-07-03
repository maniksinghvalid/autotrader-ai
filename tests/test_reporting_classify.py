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


def test_intent_protective_put_but_label_stock_entry_mismatches():
    # Intent was a protective put; fills show stock only -> hedge leg missing.
    from autotrader.reporting.classify import overlay_intent_mismatch
    assert overlay_intent_mismatch("PROTECTIVE_PUT", "Stock entry") == "Protective Put"


def test_intent_collar_but_label_stock_entry_mismatches():
    from autotrader.reporting.classify import overlay_intent_mismatch
    assert overlay_intent_mismatch("COLLAR", "Stock entry") == "Collar"


def test_intent_covered_call_but_label_stock_entry_mismatches():
    from autotrader.reporting.classify import overlay_intent_mismatch
    assert overlay_intent_mismatch("COVERED_CALL", "Stock entry") == "Covered Call"


def test_intent_matches_actual_no_mismatch():
    # Fills produced the real protective put -> structural label agrees.
    from autotrader.reporting.classify import overlay_intent_mismatch
    assert overlay_intent_mismatch("PROTECTIVE_PUT", "Protective Put") is None


def test_collar_intent_partial_still_mismatches_covered_call():
    # Collar intended; only the short call filled -> structurally a covered call,
    # which still lacks the protective put -> mismatch on the missing hedge.
    from autotrader.reporting.classify import overlay_intent_mismatch
    assert overlay_intent_mismatch("COLLAR", "Covered Call") == "Collar"


def test_no_prefix_never_mismatches():
    from autotrader.reporting.classify import overlay_intent_mismatch
    assert overlay_intent_mismatch("", "Stock entry") is None


def test_unknown_prefix_never_mismatches():
    from autotrader.reporting.classify import overlay_intent_mismatch
    assert overlay_intent_mismatch("MOMENTUM", "Stock entry") is None
