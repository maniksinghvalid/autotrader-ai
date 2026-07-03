from datetime import date

from autotrader.config import holiday_horizon_warning, load_risk_config


def test_signals_enabled_default_true(monkeypatch):
    monkeypatch.delenv("RISK_SIGNALS_ENABLED", raising=False)
    assert load_risk_config().signals_enabled is True


def test_signals_can_be_disabled(monkeypatch):
    monkeypatch.setenv("RISK_SIGNALS_ENABLED", "0")
    assert load_risk_config().signals_enabled is False


def test_default_holidays_extend_past_2026(monkeypatch):
    monkeypatch.delenv("RISK_MARKET_HOLIDAYS", raising=False)
    assert max(load_risk_config().market_holidays).year >= 2027


def test_horizon_warning_when_calendar_nearly_expired():
    hols = frozenset({date(2026, 12, 25)})
    assert holiday_horizon_warning(hols, today=date(2026, 11, 20)) is not None
    assert holiday_horizon_warning(hols, today=date(2026, 6, 1)) is None
