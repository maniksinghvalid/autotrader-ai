"""V1 rig: async fills + cancel latency make live-only races testable offline."""
from autotrader.domain import OrderRequest, OrderState
from autotrader.sim_broker import SimBroker


def _buy(cid="c1", qty=10):
    return OrderRequest(symbol="US.TEST", side="BUY", qty=qty,
                        order_type="MARKET", limit_price=None, client_order_id=cid)


def test_sync_default_unchanged():
    b = SimBroker({"US.TEST": 100.0})
    ack = b.place_order(_buy())
    assert ack.state is OrderState.FILLED          # today's behavior preserved


def test_async_market_acks_submitted_then_fills_after_latency():
    b = SimBroker({"US.TEST": 100.0}, fill_latency_ticks=2)
    ack = b.place_order(_buy())
    assert ack.state is OrderState.SUBMITTED
    assert any(o.client_order_id == "c1" for o in b.get_open_orders())
    assert b.get_account().position_qty("US.TEST") == 0
    b.tick_market()
    assert any(o.client_order_id == "c1" for o in b.get_open_orders())  # still working
    b.tick_market()
    assert not any(o.client_order_id == "c1" for o in b.get_open_orders())
    assert b.get_account().position_qty("US.TEST") == 10
    fills = b.reconcile_fills(None)
    assert len(fills) == 1 and fills[0].symbol == "US.TEST"


def test_cancel_latency_fill_wins_race():
    # Fill due on the same tick as the cancel -> fill wins (live race).
    b = SimBroker({"US.TEST": 100.0}, fill_latency_ticks=1, cancel_latency_ticks=1)
    ack = b.place_order(_buy())
    b.cancel_order(ack.broker_order_id)   # cancel REQUEST accepted, not effective yet
    assert any(o.client_order_id == "c1" for o in b.get_open_orders())
    b.tick_market()                        # fill matures BEFORE cancel
    assert b.get_account().position_qty("US.TEST") == 10   # double-fill hazard made visible


def test_cancel_before_fill_matures_kills_order():
    b = SimBroker({"US.TEST": 100.0}, fill_latency_ticks=3, cancel_latency_ticks=1)
    ack = b.place_order(_buy())
    b.cancel_order(ack.broker_order_id)
    b.tick_market()                        # cancel matures (fill needs 2 more)
    assert not any(o.client_order_id == "c1" for o in b.get_open_orders())
    b.tick_market(); b.tick_market()
    assert b.get_account().position_qty("US.TEST") == 0    # never fills
