from datetime import date, timedelta

from autotrader.moomoo_broker import _chain_windows


def _today():
    return date(2026, 6, 18)


def test_filters_to_dte_window():
    t = _today()
    expiries = [t + timedelta(days=10),   # below dte_min
                t + timedelta(days=200),  # in window
                t + timedelta(days=900)]  # above dte_max
    wins = _chain_windows(expiries, t, 180, 730)
    assert wins == [(t + timedelta(days=200), t + timedelta(days=200))]


def test_groups_nearby_expiries_within_max_span():
    t = _today()
    expiries = [t + timedelta(days=200), t + timedelta(days=220),
                t + timedelta(days=400)]
    wins = _chain_windows(expiries, t, 180, 730, max_span=30)
    # 200 and 220 are within 30 days -> one window; 400 is its own.
    assert wins == [(t + timedelta(days=200), t + timedelta(days=220)),
                    (t + timedelta(days=400), t + timedelta(days=400))]


def test_empty_when_nothing_in_window():
    t = _today()
    assert _chain_windows([t + timedelta(days=5)], t, 180, 730) == []
