import json
import threading
import pytest
from autotrader.domain import OrderRequest, OrderState, AccountSnapshot
from autotrader.config import RiskConfig
from autotrader.sim_broker import SimBroker
from autotrader.router import OrderRouter


def _cfg():
    return RiskConfig(trading_env="PAPER", min_confidence=0.6, max_order_notional=2000,
                      max_position_qty=100, daily_loss_limit=500, max_gross_exposure=10000,
                      allowed_symbols=frozenset({"US.AAPL"}))


def _req(cid="c1", qty=5):
    return OrderRequest(symbol="US.AAPL", side="BUY", qty=qty, order_type="LIMIT",
                        limit_price=100.0, client_order_id=cid)


def test_router_writes_audit_before_placing(tmp_path):
    audit = tmp_path / "audit.jsonl"
    b = SimBroker(quotes={"US.AAPL": 100.0})
    r = OrderRouter(b, audit_path=str(audit))
    ack = r.submit(_req())
    assert ack.state is OrderState.FILLED
    lines = audit.read_text().strip().splitlines()
    actions = [json.loads(l)["action"] for l in lines]
    assert actions[0] == "intent"          # written FIRST, before the broker call
    assert "ack" in actions


def test_router_halts_if_audit_unwritable(tmp_path):
    b = SimBroker(quotes={"US.AAPL": 100.0})
    # point audit at a path whose parent is a file, so opening fails (E18)
    bad_parent = tmp_path / "afile"
    bad_parent.write_text("x")
    r = OrderRouter(b, audit_path=str(bad_parent / "audit.jsonl"))
    with pytest.raises(RuntimeError):
        r.submit(_req())
    assert b.get_account().position_qty("US.AAPL") == 0  # no order placed


def test_router_dedupes_same_client_order_id(tmp_path):
    b = SimBroker(quotes={"US.AAPL": 100.0})
    r = OrderRouter(b, audit_path=str(tmp_path / "a.jsonl"))
    r.submit(_req(cid="dup"))
    r.submit(_req(cid="dup"))
    assert b.get_account().position_qty("US.AAPL") == 5  # filled once


def test_make_client_order_id_is_stable_for_same_inputs():
    a = OrderRouter.make_client_order_id("US.AAPL", "BUY", 5, "sig-42")
    b = OrderRouter.make_client_order_id("US.AAPL", "BUY", 5, "sig-42")
    c = OrderRouter.make_client_order_id("US.AAPL", "BUY", 6, "sig-42")
    assert a == b and a != c
