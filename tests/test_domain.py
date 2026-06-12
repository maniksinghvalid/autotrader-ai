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
