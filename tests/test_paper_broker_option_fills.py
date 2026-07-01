"""Characterization tests for broker option MARKET order fill behavior (spec open item #1).

Tests verify that SimBroker fills option MARKET orders to FILLED when auto_fill=True
(like stock MARKET orders), and rests when auto_fill=False; and that MoomooBroker maps
successful MARKET orders to SUBMITTED (never FILLED at placement), following the live
async fill semantics. This pins the meaning of "confirmed hedge = FILLED within a
bounded fill-poll" in §2.B: paper fills at ack (0 polls), live acks SUBMITTED and
confirms only if the order leaves the open-orders book within the poll window.
"""
from datetime import date
from autotrader.domain import (
    OptionContract, OrderRequest, OrderState,
)
from autotrader.sim_broker import SimBroker


_OPT = OptionContract(
    underlying="US.AAPL", expiry=date(2026, 7, 21), strike=190.0,
    right="PUT", code="US.AAPL260721P190000", multiplier=100)


def _opt_req(side="BUY"):
    return OrderRequest(
        symbol="US.AAPL260721P190000", side=side, qty=1,
        order_type="MARKET", limit_price=None,
        client_order_id=f"opt-{side}", option=_OPT,
        position_effect="OPEN", correlation_id="ov-1")


def test_sim_broker_fills_option_market_order_when_autofill():
    """CHARACTERIZATION (spec open item #1): the paper SimBroker DOES fill an
    option MARKET order to FILLED, same as a stock MARKET order."""
    b = SimBroker(quotes={"US.AAPL260721P190000": 1.4}, auto_fill=True)
    ack = b.place_order(_opt_req("BUY"))
    assert ack.state is OrderState.FILLED
    assert b.get_account().position_qty("US.AAPL260721P190000") == 1


def test_sim_broker_rests_option_market_order_when_not_autofill():
    """With auto_fill=False the same option MARKET order RESTS (SUBMITTED),
    modelling a paper venue that does not immediately fill options."""
    b = SimBroker(quotes={"US.AAPL260721P190000": 1.4}, auto_fill=False)
    ack = b.place_order(_opt_req("BUY"))
    assert ack.state is OrderState.SUBMITTED
    assert b.get_account().position_qty("US.AAPL260721P190000") == 0


def test_moomoo_broker_market_returns_submitted_not_filled_by_contract():
    """CHARACTERIZATION: MoomooBroker.place_order maps a successful MARKET
    order to OrderState.SUBMITTED (async live fill), never FILLED at placement
    — for options exactly as for equities (moomoo_broker.py:206-222). Pinned
    by reading the module source so the test stays offline (no OpenD)."""
    import inspect
    from autotrader import moomoo_broker
    src = inspect.getsource(moomoo_broker.MoomooBroker.place_order)
    assert "OrderState.SUBMITTED" in src
    assert "OrderState.FILLED" not in src  # no path returns FILLED at placement
