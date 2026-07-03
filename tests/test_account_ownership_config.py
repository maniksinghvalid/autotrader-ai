import pytest


def test_default_is_sole(monkeypatch):
    monkeypatch.delenv("RISK_ACCOUNT_OWNERSHIP", raising=False)
    from autotrader.config import load_risk_config
    assert load_risk_config().account_ownership == "SOLE"


def test_shared_paper_ok(monkeypatch):
    monkeypatch.setenv("RISK_ACCOUNT_OWNERSHIP", "shared")
    from autotrader.config import load_risk_config
    assert load_risk_config().account_ownership == "SHARED"


def test_live_plus_shared_refused(monkeypatch):
    monkeypatch.setenv("RISK_TRADING_ENV", "LIVE")
    monkeypatch.setenv("RISK_ACCOUNT_OWNERSHIP", "SHARED")
    from autotrader.config import load_risk_config
    with pytest.raises(ValueError, match="SHARED"):
        load_risk_config()


def test_bogus_value_refused(monkeypatch):
    monkeypatch.setenv("RISK_ACCOUNT_OWNERSHIP", "MAYBE")
    from autotrader.config import load_risk_config
    with pytest.raises(ValueError):
        load_risk_config()


def test_sim_fills_carry_client_order_id():
    from autotrader.domain import OrderRequest
    from autotrader.sim_broker import SimBroker
    b = SimBroker({"US.TEST": 100.0})
    b.place_order(OrderRequest(symbol="US.TEST", side="BUY", qty=1,
                               order_type="MARKET", limit_price=None,
                               client_order_id="at-abc"))
    assert b.reconcile_fills(None)[0].client_order_id == "at-abc"


def test_db_owned_symbols(tmp_path):
    from autotrader.db import DB
    db = DB(str(tmp_path / "t.db"))
    db.record_trade(client_order_id="at-1", symbol="US.MARA", side="BUY", qty=1,
                    order_type="MARKET", limit_price=None,
                    broker_order_id="b1", state="FILLED")
    assert db.owned_symbols() == {"US.MARA"}
