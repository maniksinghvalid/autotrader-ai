"""Clock tests. FixedClock makes every time-dependent module deterministic."""
from datetime import datetime
from zoneinfo import ZoneInfo


def test_fixed_clock_returns_the_preset_time():
    from autotrader.clock import FixedClock
    dt = datetime(2026, 6, 12, 9, 45, tzinfo=ZoneInfo("America/New_York"))
    c = FixedClock(dt)
    assert c.now_est() == dt


def test_fixed_clock_set_updates_the_time():
    from autotrader.clock import FixedClock
    ny = ZoneInfo("America/New_York")
    c = FixedClock(datetime(2026, 6, 12, 8, 30, tzinfo=ny))
    c.set(datetime(2026, 6, 12, 16, 15, tzinfo=ny))
    assert c.now_est().hour == 16 and c.now_est().minute == 15


def test_real_clock_is_timezone_aware_new_york():
    from autotrader.clock import Clock
    now = Clock().now_est()
    assert now.tzinfo is not None
    assert "New_York" in str(now.tzinfo)
