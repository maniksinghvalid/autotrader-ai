import pytest
from autotrader.domain import OrderRequest, OrderState
from autotrader.sim_broker import SimBroker


def _req(cid="c1", qty=10):
    return OrderRequest(symbol="US.AAPL", side="BUY", qty=qty, order_type="LIMIT",
                        limit_price=100.0, client_order_id=cid)


def test_sim_broker_ready_and_quote():
    b = SimBroker(quotes={"US.AAPL": 100.0})
    assert b.is_ready() is True
    assert b.get_quote("US.AAPL") == 100.0


def test_place_order_fills_and_updates_position():
    b = SimBroker(quotes={"US.AAPL": 100.0}, cash=10000.0)
    ack = b.place_order(_req(qty=10))
    assert ack.state is OrderState.FILLED
    assert ack.broker_order_id is not None
    snap = b.get_account()
    assert snap.position_qty("US.AAPL") == 10
    assert snap.cash == pytest.approx(9000.0)
    assert snap.stale is False


def test_place_order_is_idempotent_on_duplicate_client_order_id():
    b = SimBroker(quotes={"US.AAPL": 100.0}, cash=10000.0)
    a1 = b.place_order(_req(cid="dup", qty=10))
    a2 = b.place_order(_req(cid="dup", qty=10))  # same cid -> no double fill
    assert a1.broker_order_id == a2.broker_order_id
    assert b.get_account().position_qty("US.AAPL") == 10


def test_cancel_all_clears_open_orders():
    b = SimBroker(quotes={"US.AAPL": 100.0}, cash=10000.0, auto_fill=False)
    b.place_order(_req(cid="open1"))
    assert len(b.get_open_orders()) == 1
    b.cancel_all()
    assert b.get_open_orders() == []


def test_reconcile_fills_returns_dedupable_fills():
    b = SimBroker(quotes={"US.AAPL": 100.0}, cash=10000.0)
    b.place_order(_req(cid="c1"))
    fills = b.reconcile_fills(since=None)
    assert len(fills) == 1
    assert fills[0].fill_id  # stable id for dedupe


def _ts_req(cid="ts1", qty=10):
    return OrderRequest(symbol="US.AAPL", side="SELL", qty=qty,
                        order_type="TRAILING_STOP", limit_price=None,
                        client_order_id=cid, trail_percent=5.0)


def test_trailing_stop_rests_and_does_not_fill():
    b = SimBroker(quotes={"US.AAPL": 100.0}, cash=10000.0)  # auto_fill defaults True
    ack = b.place_order(_ts_req())
    assert ack.state is OrderState.SUBMITTED              # resting, NOT filled
    assert b.get_account().position_qty("US.AAPL") == 0   # no position change
    assert len(b.get_open_orders()) == 1                  # appears as a working order


def test_trailing_stop_cleared_by_cancel_all():
    b = SimBroker(quotes={"US.AAPL": 100.0}, cash=10000.0)
    b.place_order(_ts_req())
    assert len(b.get_open_orders()) == 1
    b.cancel_all()
    assert b.get_open_orders() == []
