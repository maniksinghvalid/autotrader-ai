import dataclasses
import pytest
from autotrader.config import RiskConfig, load_risk_config


def test_risk_config_is_frozen():
    cfg = RiskConfig(trading_env="PAPER", min_confidence=0.6, max_order_notional=2000,
                     max_position_qty=100, daily_loss_limit=500, max_gross_exposure=10000,
                     allowed_symbols=frozenset({"US.AAPL"}))
    with pytest.raises(dataclasses.FrozenInstanceError):
        cfg.max_order_notional = 99999  # type: ignore[misc]


def test_loader_reads_env_and_defaults_to_paper(monkeypatch):
    for k in ("RISK_TRADING_ENV", "RISK_MIN_CONFIDENCE", "RISK_MAX_ORDER_NOTIONAL",
              "RISK_MAX_POSITION_QTY", "RISK_DAILY_LOSS_LIMIT", "RISK_MAX_GROSS_EXPOSURE",
              "RISK_ALLOWED_SYMBOLS"):
        monkeypatch.delenv(k, raising=False)
    cfg = load_risk_config()
    assert cfg.trading_env == "PAPER"          # safe default
    assert cfg.allowed_symbols == frozenset()  # empty allow-list = nothing tradable
    assert cfg.min_confidence == 0.6


def test_loader_parses_symbols_and_live_requires_explicit_flag(monkeypatch):
    monkeypatch.setenv("RISK_ALLOWED_SYMBOLS", "US.AAPL, us.msft ,US.NIO")
    monkeypatch.setenv("RISK_TRADING_ENV", "live")
    cfg = load_risk_config()
    assert cfg.allowed_symbols == frozenset({"US.AAPL", "US.MSFT", "US.NIO"})
    assert cfg.trading_env == "LIVE"  # normalized uppercase; routing is enforced in risk_core


def test_loader_reads_trailing_stop_pct(monkeypatch):
    monkeypatch.delenv("RISK_TRAILING_STOP_PCT", raising=False)
    assert load_risk_config().trailing_stop_pct == 0.0   # disabled by default
    monkeypatch.setenv("RISK_TRAILING_STOP_PCT", "5.0")
    assert load_risk_config().trailing_stop_pct == 5.0
