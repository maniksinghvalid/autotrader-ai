from autotrader.reporting.capital_flow import CapitalFlow, compute_capital_flow


def test_mixed_day_collected_paid_and_net():
    fills = [
        ("US.CLOV", "BUY", 200, 4.99),                 # stock buy  -> -998
        ("US.CLOV260821C7000", "SELL", 2, 0.20),       # call credit -> +40
        ("US.SPCE", "BUY", 100, 3.33),                 # stock buy  -> -333
        ("US.SPCE260821P3000", "BUY", 1, 0.48),        # put debit  -> -48
        ("US.SCHF", "SELL", 100, 28.27),               # stock sell -> +2827
    ]
    cf = compute_capital_flow(fills)
    assert abs(cf.premium_collected - 40.0) < 1e-6
    assert abs(cf.premium_paid - 48.0) < 1e-6
    assert abs(cf.net_cash_deployed - (-998 + 40 - 333 - 48 + 2827)) < 1e-6


def test_no_options_is_zero_premium():
    cf = compute_capital_flow([("US.SCHF", "SELL", 100, 28.27)])
    assert cf.premium_collected == 0.0 and cf.premium_paid == 0.0
    assert abs(cf.net_cash_deployed - 2827.0) < 1e-6
