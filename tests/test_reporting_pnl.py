from autotrader.reporting.pnl import realized_from_fills

DAY = "2026-06-16"


def test_avg_cost_realized_single_symbol():
    # Buy 10@100 then 10@120 -> avg 110. Sell 5@130 today -> (130-110)*5 = 100.
    fills = [
        ("US.AAPL", "BUY", 10, 100.0, "2026-06-15T14:00:00+00:00"),
        ("US.AAPL", "BUY", 10, 120.0, "2026-06-16T14:00:00+00:00"),
        ("US.AAPL", "SELL", 5, 130.0, "2026-06-16T15:00:00+00:00"),
    ]
    assert abs(realized_from_fills(fills, DAY) - 100.0) < 1e-6


def test_realized_none_when_no_sell_today():
    fills = [("US.AAPL", "BUY", 10, 100.0, "2026-06-16T14:00:00+00:00")]
    assert realized_from_fills(fills, DAY) is None


def test_realized_ignores_prior_day_sells():
    fills = [
        ("US.AAPL", "BUY", 10, 100.0, "2026-06-14T14:00:00+00:00"),
        ("US.AAPL", "SELL", 10, 150.0, "2026-06-15T14:00:00+00:00"),  # prior day
    ]
    assert realized_from_fills(fills, DAY) is None


def test_realized_multi_symbol_summed():
    fills = [
        ("US.AAPL", "BUY", 10, 100.0, "2026-06-16T14:00:00+00:00"),
        ("US.AAPL", "SELL", 10, 110.0, "2026-06-16T15:00:00+00:00"),   # +100
        ("US.MARA", "BUY", 100, 13.0, "2026-06-16T14:00:00+00:00"),
        ("US.MARA", "SELL", 100, 12.60, "2026-06-16T15:00:00+00:00"),  # -40
    ]
    assert abs(realized_from_fills(fills, DAY) - 60.0) < 1e-6
