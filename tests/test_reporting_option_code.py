from datetime import date

from autotrader.reporting.option_code import ParsedOption, parse_option_code


def test_parses_call_code():
    p = parse_option_code("US.CLOV260821C7000")
    assert p == ParsedOption(underlying="US.CLOV", expiry=date(2026, 8, 21),
                             strike=7.00, right="CALL", multiplier=100)


def test_parses_put_code():
    p = parse_option_code("US.SPCE260821P3000")
    assert p.underlying == "US.SPCE" and p.right == "PUT"
    assert p.strike == 3.00 and p.expiry == date(2026, 8, 21)


def test_parses_fractional_strike():
    p = parse_option_code("US.DIVO260821C47410")
    assert p.strike == 47.41 and p.right == "CALL"


def test_plain_stock_symbol_returns_none():
    assert parse_option_code("US.SCHF") is None


def test_malformed_code_returns_none():
    assert parse_option_code("") is None
    assert parse_option_code("US.AAPL26XXC1000") is None
    assert parse_option_code("US.AAPL260821X1000") is None  # bad right letter
