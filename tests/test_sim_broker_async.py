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


def test_cancel_all_purges_pending_async_state():
    # Verify that cancel_all() clears _pending_fills and _pending_cancels,
    # so a matured tick after cancel_all() never materializes a fill.
    b = SimBroker({"US.TEST": 100.0}, fill_latency_ticks=2)
    initial_cash = b.get_account().cash
    ack = b.place_order(_buy())
    assert ack.state is OrderState.SUBMITTED
    assert any(o.client_order_id == "c1" for o in b.get_open_orders())
    assert b.get_account().cash == initial_cash  # not filled yet
    b.cancel_all()
    assert not any(o.client_order_id == "c1" for o in b.get_open_orders())
    # Two tick_market() calls should NOT fill the order
    b.tick_market()
    assert b.get_account().position_qty("US.TEST") == 0
    assert b.get_account().cash == initial_cash  # cash unchanged
    b.tick_market()
    assert b.get_account().position_qty("US.TEST") == 0
    assert b.get_account().cash == initial_cash  # cash still unchanged
    assert len(b.reconcile_fills(None)) == 0     # no fills at all


def test_async_marketable_limit_fills_at_limit_price():
    # Verify marketable LIMIT orders route through async path and fill at limit_price.
    # With spread_bps=100 (1%), ref=100: bid=99.0, ask=101.0.
    # Set limit_price = ask + 1.0 to diverge from market fill price.
    # If code regressed to _market_fill_price, it would fill at 101.0 instead of 102.0.
    b = SimBroker({"US.TEST": 100.0}, fill_latency_ticks=2, spread_bps=100.0)
    bid, ask = b.get_touch("US.TEST")
    assert bid < 100.0 < ask  # verify spread is present
    limit_price = ask + 1.0  # strictly above ask to diverge from market fill price
    req = OrderRequest(symbol="US.TEST", side="BUY", qty=10,
                       order_type="LIMIT", limit_price=limit_price,
                       client_order_id="limit1")
    ack = b.place_order(req)
    assert ack.state is OrderState.SUBMITTED
    assert any(o.client_order_id == "limit1" for o in b.get_open_orders())
    initial_cash = b.get_account().cash
    b.tick_market()
    assert any(o.client_order_id == "limit1" for o in b.get_open_orders())  # still pending
    b.tick_market()
    assert not any(o.client_order_id == "limit1" for o in b.get_open_orders())
    # Verify it filled at the limit price, not at the market ask
    fills = b.reconcile_fills(None)
    assert len(fills) == 1
    assert fills[0].price == limit_price  # must be ask + 1.0, not ask
    assert fills[0].qty == 10
    assert b.get_account().position_qty("US.TEST") == 10
    expected_cash = initial_cash - (10 * limit_price)
    assert b.get_account().cash == expected_cash
