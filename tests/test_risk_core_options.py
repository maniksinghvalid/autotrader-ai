import pytest
from datetime import date

from autotrader.config import load_risk_config, RiskConfig
from autotrader.domain import (
    AccountSnapshot, OptionContract, OrderRequest, Position,
)
from autotrader.risk_core import evaluate


def _cfg(**over):
    base = dict(trading_env="PAPER", min_confidence=0.6, max_order_notional=2000,
                max_position_qty=100, daily_loss_limit=500, max_gross_exposure=50000,
                allowed_symbols=frozenset({"US.AAPL"}), daily_loss_halt=1000,
                allowed_overlays=frozenset({"COVERED_CALL", "PROTECTIVE_PUT"}),
                max_option_contracts=5, max_option_premium_per_trade=800.0)
    base.update(over)
    return RiskConfig(**base)


def _snap(aapl_shares=100):
    pos = (Position("US.AAPL", aapl_shares, 200.0),) if aapl_shares else ()
    return AccountSnapshot(cash=50000, total_assets=70000, day_pnl=0.0,
                           stale=False, positions=pos)


def _call(qty=1):
    c = OptionContract(underlying="US.AAPL", expiry=date(2026, 7, 17), strike=210,
                       right="CALL", code="US.AAPL260717C210000")
    return OrderRequest(symbol=c.code, side="SELL", qty=qty, order_type="MARKET",
                        limit_price=None, client_order_id="at-c", option=c,
                        position_effect="OPEN", correlation_id="k")


def _put(qty=1):
    c = OptionContract(underlying="US.AAPL", expiry=date(2026, 7, 17), strike=190,
                       right="PUT", code="US.AAPL260717P190000")
    return OrderRequest(symbol=c.code, side="BUY", qty=qty, order_type="MARKET",
                        limit_price=None, client_order_id="at-p", option=c,
                        position_effect="OPEN", correlation_id="k")


def test_covered_short_call_approved():
    d = evaluate(_call(qty=1), _snap(aapl_shares=100), _cfg(), ref_price=1.5)
    assert d.approved, d.reason


def test_uncovered_short_call_rejected():
    d = evaluate(_call(qty=1), _snap(aapl_shares=0), _cfg(), ref_price=1.5)
    assert not d.approved and "uncovered" in d.reason.lower()


def test_short_call_more_contracts_than_covered_rejected():
    d = evaluate(_call(qty=2), _snap(aapl_shares=100), _cfg(), ref_price=1.5)
    assert not d.approved and "uncovered" in d.reason.lower()


def test_contracts_over_cap_rejected():
    d = evaluate(_call(qty=6), _snap(aapl_shares=600), _cfg(), ref_price=1.5)
    assert not d.approved and "contracts" in d.reason.lower()


def test_long_put_premium_cap_rejected():
    d = evaluate(_put(qty=1), _snap(), _cfg(), ref_price=9.0)
    assert not d.approved and "premium" in d.reason.lower()


def test_long_put_within_cap_approved():
    d = evaluate(_put(qty=1), _snap(), _cfg(), ref_price=1.4)
    assert d.approved, d.reason


def test_option_underlying_not_in_allowlist_rejected():
    d = evaluate(_put(qty=1), _snap(), _cfg(allowed_symbols=frozenset({"US.MSFT"})),
                 ref_price=1.4)
    assert not d.approved and "allow-list" in d.reason.lower()


def test_option_blocked_when_contracts_cap_zero():
    d = evaluate(_call(qty=1), _snap(), _cfg(max_option_contracts=0), ref_price=1.5)
    assert not d.approved and "contracts" in d.reason.lower()


def test_option_rejected_on_stale_snapshot():
    snap = AccountSnapshot(cash=50000, total_assets=70000, day_pnl=0.0,
                           stale=True, positions=(Position("US.AAPL", 100, 200.0),))
    d = evaluate(_call(qty=1), snap, _cfg(), ref_price=1.5)
    assert not d.approved and "stale" in d.reason.lower()


def test_option_config_defaults_off(monkeypatch):
    monkeypatch.setenv("RISK_ALLOWED_SYMBOLS", "US.AAPL")
    cfg = load_risk_config()
    assert cfg.allowed_overlays == frozenset()
    assert cfg.max_option_contracts == 0
    assert cfg.max_option_premium_per_trade == 0.0
    assert cfg.option_target_delta == 0.30
    assert cfg.option_dte_min == 30 and cfg.option_dte_max == 45
    assert cfg.option_dte_to_close == 7
    assert cfg.option_profit_target_pct == 0.5


def test_option_config_parses_env(monkeypatch):
    monkeypatch.setenv("RISK_ALLOWED_SYMBOLS", "US.AAPL")
    monkeypatch.setenv("RISK_ALLOWED_OVERLAYS", "covered_call, protective_put")
    monkeypatch.setenv("RISK_MAX_OPTION_CONTRACTS", "5")
    monkeypatch.setenv("RISK_MAX_OPTION_PREMIUM_PER_TRADE", "800")
    cfg = load_risk_config()
    assert cfg.allowed_overlays == frozenset({"COVERED_CALL", "PROTECTIVE_PUT"})
    assert cfg.max_option_contracts == 5
    assert cfg.max_option_premium_per_trade == 800.0


def test_option_config_rejects_inverted_dte_window(monkeypatch):
    monkeypatch.setenv("RISK_ALLOWED_SYMBOLS", "US.AAPL")
    monkeypatch.setenv("RISK_OPTION_DTE_MIN", "60")
    monkeypatch.setenv("RISK_OPTION_DTE_MAX", "45")
    with pytest.raises(ValueError):
        load_risk_config()


# ---------------------------------------------------------------------------
# C1 — stacked short coverage: existing short contracts must consume coverage
# ---------------------------------------------------------------------------

def test_short_call_rejected_when_existing_shorts_consume_coverage():
    # 200 shares already cover 2 short calls; a 3rd contract would be naked.
    snap = AccountSnapshot(
        cash=50000, total_assets=70000, day_pnl=0.0, stale=False,
        positions=(Position("US.AAPL", 200, 200.0),
                   Position("US.AAPL260717C210000", -2, 1.5)))  # 2 short calls already
    d = evaluate(_call(qty=1), snap, _cfg(), ref_price=1.5)
    assert not d.approved and "uncovered" in d.reason.lower()


def test_short_call_approved_when_coverage_remains():
    # 300 shares, 2 short calls already (cover 200) -> 100 shares free covers 1 more.
    snap = AccountSnapshot(
        cash=50000, total_assets=70000, day_pnl=0.0, stale=False,
        positions=(Position("US.AAPL", 300, 200.0),
                   Position("US.AAPL260717C210000", -2, 1.5)))
    d = evaluate(_call(qty=1), snap, _cfg(), ref_price=1.5)
    assert d.approved, d.reason


# ---------------------------------------------------------------------------
# I1a — daily-loss limit blocks option OPEN legs
# ---------------------------------------------------------------------------

def test_option_open_blocked_on_daily_loss_breach():
    snap = AccountSnapshot(cash=50000, total_assets=70000, day_pnl=-600.0,
                           stale=False, positions=(Position("US.AAPL", 200, 200.0),))
    d = evaluate(_call(qty=1), snap, _cfg(daily_loss_limit=500), ref_price=1.5)
    assert not d.approved and "daily loss" in d.reason.lower()
