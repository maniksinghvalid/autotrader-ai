import dataclasses
import pytest
from autotrader.domain import (
    Signal, OrderRequest, OrderAck, Fill, Position, AccountSnapshot,
    OrderState, BrokerError, BrokerErrorKind,
)


def test_signal_is_frozen_and_validates_confidence():
    s = Signal(symbol="US.AAPL", direction="BUY", confidence=0.8, rationale="x")
    assert s.symbol == "US.AAPL"
    with pytest.raises(dataclasses.FrozenInstanceError):
        s.confidence = 0.1  # type: ignore[misc]
    with pytest.raises(ValueError):
        Signal(symbol="US.AAPL", direction="BUY", confidence=1.5, rationale="x")
    with pytest.raises(ValueError):
        Signal(symbol="US.AAPL", direction="HOLD", confidence=0.5, rationale="x")


def test_signal_stop_price_optional_and_validated():
    # Default: no stop carried.
    assert Signal(symbol="US.AAPL", direction="BUY", confidence=0.8,
                  rationale="x").stop_price is None
    # A valid positive stop is accepted and carried.
    s = Signal(symbol="US.AAPL", direction="BUY", confidence=0.8, rationale="x",
               stop_price=95.0)
    assert s.stop_price == 95.0
    # Non-positive / non-finite stops are rejected.
    with pytest.raises(ValueError):
        Signal(symbol="US.AAPL", direction="BUY", confidence=0.8, rationale="x",
               stop_price=0.0)
    with pytest.raises(ValueError):
        Signal(symbol="US.AAPL", direction="BUY", confidence=0.8, rationale="x",
               stop_price=float("nan"))


def test_order_request_requires_positive_qty_and_limit_price_rules():
    r = OrderRequest(symbol="US.AAPL", side="BUY", qty=10, order_type="MARKET",
                     limit_price=None, client_order_id="abc")
    assert r.qty == 10
    with pytest.raises(ValueError):
        OrderRequest(symbol="US.AAPL", side="BUY", qty=0, order_type="MARKET",
                     limit_price=None, client_order_id="abc")
    with pytest.raises(ValueError):
        OrderRequest(symbol="US.AAPL", side="BUY", qty=10, order_type="LIMIT",
                     limit_price=None, client_order_id="abc")  # LIMIT needs a price
    with pytest.raises(ValueError):
        OrderRequest(symbol="US.AAPL", side="HOLD", qty=10, order_type="MARKET",
                     limit_price=None, client_order_id="abc")  # side must be BUY/SELL


def test_order_ack_raw_is_read_only():
    ack = OrderAck("c", None, OrderState.SUBMITTED, {"k": 1})
    assert ack.raw["k"] == 1
    with pytest.raises(TypeError):
        ack.raw["x"] = 1  # type: ignore[index]


def test_account_snapshot_exposure_and_position_lookup():
    snap = AccountSnapshot(
        cash=5000.0, total_assets=7000.0, day_pnl=-100.0, stale=False,
        positions=(Position(symbol="US.AAPL", qty=10, avg_price=200.0),),
    )
    assert snap.position_qty("US.AAPL") == 10
    assert snap.position_qty("US.MSFT") == 0
    assert snap.gross_exposure() == pytest.approx(2000.0)


def test_order_state_unknown_is_not_terminal_success():
    assert OrderState.UNKNOWN is not OrderState.FILLED
    assert OrderState.FILLED.is_success() is True
    assert OrderState.UNKNOWN.is_success() is False
    assert OrderState.REJECTED.is_success() is False


def test_broker_error_carries_kind():
    err = BrokerError(BrokerErrorKind.RATE_LIMIT, "too fast")
    assert err.kind is BrokerErrorKind.RATE_LIMIT
    assert "too fast" in str(err)


def test_trailing_stop_order_requires_trail_percent_and_no_limit():
    r = OrderRequest(symbol="US.AAPL", side="SELL", qty=10, order_type="TRAILING_STOP",
                     limit_price=None, client_order_id="ts", trail_percent=5.0)
    assert r.trail_percent == 5.0
    with pytest.raises(ValueError):  # missing trail_percent
        OrderRequest(symbol="US.AAPL", side="SELL", qty=10, order_type="TRAILING_STOP",
                     limit_price=None, client_order_id="ts")
    with pytest.raises(ValueError):  # TRAILING_STOP must not carry a limit_price
        OrderRequest(symbol="US.AAPL", side="SELL", qty=10, order_type="TRAILING_STOP",
                     limit_price=99.0, client_order_id="ts", trail_percent=5.0)


def test_non_trailing_order_defaults_trail_percent_to_none():
    r = OrderRequest(symbol="US.AAPL", side="BUY", qty=1, order_type="MARKET",
                     limit_price=None, client_order_id="m")
    assert r.trail_percent is None
