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


def test_loader_reads_sizing_knobs_with_safe_defaults(monkeypatch):
    for k in ("RISK_PER_TRADE_PCT", "RISK_CONFIDENCE_SIZE_FLOOR",
              "RISK_CONFIDENCE_SIZE_CEIL"):
        monkeypatch.delenv(k, raising=False)
    cfg = load_risk_config()
    assert cfg.risk_per_trade_pct == 0.0      # sizing OFF by default -> fixed ORDER_QTY
    assert cfg.confidence_size_floor == 0.5
    assert cfg.confidence_size_ceil == 1.0
    monkeypatch.setenv("RISK_PER_TRADE_PCT", "0.01")
    monkeypatch.setenv("RISK_CONFIDENCE_SIZE_FLOOR", "0.4")
    monkeypatch.setenv("RISK_CONFIDENCE_SIZE_CEIL", "1.2")
    cfg2 = load_risk_config()
    assert cfg2.risk_per_trade_pct == 0.01
    assert cfg2.confidence_size_floor == 0.4
    assert cfg2.confidence_size_ceil == 1.2


def test_loader_reads_limit_order_knobs_with_safe_defaults(monkeypatch):
    for k in ("RISK_ORDER_CAP_BPS", "RISK_ORDER_CAP_TICKS", "RISK_LIMIT_ORDERS_ENABLED"):
        monkeypatch.delenv(k, raising=False)
    cfg = load_risk_config()
    # DEFAULT OFF -> current MARKET behavior preserved.
    assert cfg.limit_orders_enabled is False
    assert cfg.order_cap_bps == 0.0
    assert cfg.order_cap_ticks == 0.0
    monkeypatch.setenv("RISK_ORDER_CAP_BPS", "5")       # 5 bps = 0.05% of price
    monkeypatch.setenv("RISK_ORDER_CAP_TICKS", "2")     # 2 ticks floor for thin names
    monkeypatch.setenv("RISK_LIMIT_ORDERS_ENABLED", "true")
    cfg2 = load_risk_config()
    assert cfg2.limit_orders_enabled is True
    assert cfg2.order_cap_bps == 5.0
    assert cfg2.order_cap_ticks == 2.0


def test_escalation_dwell_from_env(monkeypatch):
    from autotrader.config import load_risk_config
    monkeypatch.delenv("RISK_ESCALATION_DWELL_SECONDS", raising=False)
    assert load_risk_config().escalation_dwell_seconds == 20.0
    monkeypatch.setenv("RISK_ESCALATION_DWELL_SECONDS", "5")
    assert load_risk_config().escalation_dwell_seconds == 5.0


def test_market_holidays_default_and_override(monkeypatch):
    from datetime import date
    from autotrader.config import load_risk_config
    monkeypatch.delenv("RISK_MARKET_HOLIDAYS", raising=False)
    cfg = load_risk_config()
    assert date(2026, 12, 25) in cfg.market_holidays          # shipped default
    monkeypatch.setenv("RISK_MARKET_HOLIDAYS", "2027-01-01, 2027-07-05")
    cfg = load_risk_config()
    assert cfg.market_holidays == frozenset({date(2027, 1, 1), date(2027, 7, 5)})


def test_market_holidays_bad_date_raises(monkeypatch):
    import pytest
    from autotrader.config import load_risk_config
    monkeypatch.setenv("RISK_MARKET_HOLIDAYS", "2026-13-45")
    with pytest.raises(ValueError):
        load_risk_config()
