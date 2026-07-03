import pytest
from autotrader.domain import OrderRequest, OrderState
from autotrader.sim_broker import SimBroker


def _mkt(side, cid, qty=10):
    return OrderRequest(symbol="US.AAPL", side=side, qty=qty, order_type="MARKET",
                        limit_price=None, client_order_id=cid)


def _lim(side, price, cid, qty=10):
    return OrderRequest(symbol="US.AAPL", side=side, qty=qty, order_type="LIMIT",
                        limit_price=price, client_order_id=cid)


def test_market_buy_pays_the_ask_with_spread():
    # ref 100, half-spread 50 bps -> ask 100.5, bid 99.5.
    b = SimBroker(quotes={"US.AAPL": 100.0}, cash=100000.0, spread_bps=50.0)
    ack = b.place_order(_mkt("BUY", "b1"))
    assert ack.state is OrderState.FILLED
    fill = b.reconcile_fills(None)[0]
    assert fill.price == pytest.approx(100.5)   # buys at the far touch (ask), not mid


def test_market_sell_receives_the_bid_with_spread():
    b = SimBroker(quotes={"US.AAPL": 100.0}, cash=100000.0, spread_bps=50.0)
    b._positions.clear()
    ack = b.place_order(_mkt("SELL", "s1"))
    fill = b.reconcile_fills(None)[0]
    assert fill.price == pytest.approx(99.5)     # sells at the far touch (bid)


def test_market_slippage_moves_fill_adversely_beyond_touch():
    # 50 bps spread + 20 bps slippage: buy fills at ask*(1+0.002)=100.5*1.002.
    b = SimBroker(quotes={"US.AAPL": 100.0}, cash=100000.0,
                  spread_bps=50.0, slippage_bps=20.0)
    b.place_order(_mkt("BUY", "b1"))
    assert b.reconcile_fills(None)[0].price == pytest.approx(100.5 * 1.002)


def test_marketable_limit_buy_fills_at_its_own_price():
    # limit 100.6 >= ask 100.5 -> marketable, fills at the LIMIT price (100.6).
    b = SimBroker(quotes={"US.AAPL": 100.0}, cash=100000.0, spread_bps=50.0)
    ack = b.place_order(_lim("BUY", 100.6, "l1"))
    assert ack.state is OrderState.FILLED
    assert b.reconcile_fills(None)[0].price == pytest.approx(100.6)


def test_non_marketable_limit_buy_rests_unfilled():
    # limit 100.0 < ask 100.5 -> not marketable, rests, no position change.
    b = SimBroker(quotes={"US.AAPL": 100.0}, cash=100000.0, spread_bps=50.0)
    ack = b.place_order(_lim("BUY", 100.0, "l2"))
    assert ack.state is OrderState.SUBMITTED
    assert b.get_account().position_qty("US.AAPL") == 0
    assert len(b.get_open_orders()) == 1


def test_non_marketable_limit_sell_rests_unfilled():
    b = SimBroker(quotes={"US.AAPL": 100.0}, cash=100000.0, spread_bps=50.0)
    ack = b.place_order(_lim("SELL", 100.0, "l3"))   # 100.0 > bid 99.5 -> not marketable
    assert ack.state is OrderState.SUBMITTED
    assert len(b.get_open_orders()) == 1


def test_zero_spread_zero_slippage_is_todays_behavior():
    # Defaults: bid==ask==ref==100; MARKET fills at 100 exactly (regression).
    b = SimBroker(quotes={"US.AAPL": 100.0}, cash=100000.0)
    b.place_order(_mkt("BUY", "b1"))
    assert b.reconcile_fills(None)[0].price == pytest.approx(100.0)
