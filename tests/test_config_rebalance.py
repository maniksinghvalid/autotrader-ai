import pytest

from autotrader.config import load_risk_config


def _base_env(monkeypatch):
    monkeypatch.setenv("RISK_ALLOWED_SYMBOLS", "US.AAPL,US.MSFT")
    monkeypatch.setenv("RISK_DAILY_LOSS_LIMIT", "500")


def test_defaults_when_unset(monkeypatch):
    _base_env(monkeypatch)
    for k in ("RISK_REBALANCE_ENABLED", "RISK_REBALANCE_BAND_PCT",
              "RISK_REBALANCE_MIN_NOTIONAL", "RISK_REBALANCE_CASH_BUFFER_PCT",
              "RISK_TARGET_STALENESS_HOURS", "RISK_DAILY_LOSS_HALT"):
        monkeypatch.delenv(k, raising=False)
    cfg = load_risk_config()
    assert cfg.rebalance_enabled is False
    assert cfg.rebalance_band_pct == 5.0
    assert cfg.rebalance_min_notional == 200.0
    assert cfg.rebalance_cash_buffer_pct == 10.0
    assert cfg.target_staleness_hours == 24.0
    assert cfg.daily_loss_halt == 1000.0


def test_enabled_parsed_from_env(monkeypatch):
    _base_env(monkeypatch)
    monkeypatch.setenv("RISK_REBALANCE_ENABLED", "true")
    assert load_risk_config().rebalance_enabled is True


def test_halt_must_exceed_soft_limit(monkeypatch):
    _base_env(monkeypatch)
    monkeypatch.setenv("RISK_DAILY_LOSS_LIMIT", "500")
    monkeypatch.setenv("RISK_DAILY_LOSS_HALT", "400")  # <= soft
    with pytest.raises(ValueError, match="RISK_DAILY_LOSS_HALT"):
        load_risk_config()
