import os

from autotrader.config import load_risk_config


def test_option_config_defaults_off(monkeypatch):
    monkeypatch.setenv("RISK_ALLOWED_SYMBOLS", "US.AAPL")
    cfg = load_risk_config()
    assert cfg.allowed_overlays == frozenset()
    assert cfg.max_option_contracts == 0
    assert cfg.max_option_premium_per_trade == 0.0
    assert cfg.option_target_delta == 0.30
    assert cfg.option_dte_min == 30 and cfg.option_dte_max == 45


def test_option_config_parses_env(monkeypatch):
    monkeypatch.setenv("RISK_ALLOWED_SYMBOLS", "US.AAPL")
    monkeypatch.setenv("RISK_ALLOWED_OVERLAYS", "covered_call, protective_put")
    monkeypatch.setenv("RISK_MAX_OPTION_CONTRACTS", "5")
    monkeypatch.setenv("RISK_MAX_OPTION_PREMIUM_PER_TRADE", "800")
    cfg = load_risk_config()
    assert cfg.allowed_overlays == frozenset({"COVERED_CALL", "PROTECTIVE_PUT"})
    assert cfg.max_option_contracts == 5
    assert cfg.max_option_premium_per_trade == 800.0
