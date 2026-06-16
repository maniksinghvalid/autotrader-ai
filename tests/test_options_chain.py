from datetime import date

import pytest

from autotrader.domain import (
    OptionContract, OrderRequest, OverlayType, Signal,
)


def test_option_contract_valid():
    c = OptionContract(underlying="US.AAPL", expiry=date(2026, 7, 17),
                       strike=200.0, right="CALL", code="US.AAPL260717C200000")
    assert c.multiplier == 100
    assert c.right == "CALL"


def test_option_contract_rejects_bad_strike():
    with pytest.raises(ValueError):
        OptionContract(underlying="US.AAPL", expiry=date(2026, 7, 17),
                       strike=0.0, right="CALL", code="x")


def test_option_contract_rejects_bad_right():
    with pytest.raises(ValueError):
        OptionContract(underlying="US.AAPL", expiry=date(2026, 7, 17),
                       strike=10.0, right="STRADDLE", code="x")


def test_option_contract_rejects_empty_code():
    with pytest.raises(ValueError):
        OptionContract(underlying="US.AAPL", expiry=date(2026, 7, 17),
                       strike=200.0, right="CALL", code="")


def test_option_contract_rejects_bad_multiplier():
    with pytest.raises(ValueError):
        OptionContract(underlying="US.AAPL", expiry=date(2026, 7, 17),
                       strike=200.0, right="CALL", code="US.AAPL260717C200000",
                       multiplier=0)


def test_order_request_carries_option_leg():
    c = OptionContract(underlying="US.AAPL", expiry=date(2026, 7, 17),
                       strike=200.0, right="CALL", code="US.AAPL260717C200000")
    req = OrderRequest(symbol=c.code, side="SELL", qty=1, order_type="MARKET",
                       limit_price=None, client_order_id="at-x", option=c,
                       position_effect="OPEN", correlation_id="corr-1")
    assert req.option is c
    assert req.position_effect == "OPEN"
    assert req.correlation_id == "corr-1"


def test_order_request_rejects_bad_position_effect():
    with pytest.raises(ValueError):
        OrderRequest(symbol="US.AAPL", side="BUY", qty=10, order_type="MARKET",
                     limit_price=None, client_order_id="at-z",
                     position_effect="SIDEWAYS")


def test_order_request_equity_defaults_unchanged():
    req = OrderRequest(symbol="US.AAPL", side="BUY", qty=10, order_type="MARKET",
                       limit_price=None, client_order_id="at-y")
    assert req.option is None
    assert req.position_effect == "OPEN"
    assert req.correlation_id is None


def test_signal_overlay_default_none():
    s = Signal(symbol="US.AAPL", direction="BUY", confidence=0.7, rationale="x")
    assert s.overlay is None
    assert Signal(symbol="US.AAPL", direction="SELL", confidence=0.7,
                  rationale="x", overlay=OverlayType.COVERED_CALL).overlay \
        is OverlayType.COVERED_CALL


from autotrader.signals.schema import RoutineSignalPayload
from autotrader.signals.normalize import normalize_payload


def test_overlay_flows_schema_to_signal():
    raw = ('{"routine_id":"r1","timestamp":"2026-06-15T13:00:00Z",'
           '"signal_changes":[{"ticker":"CLOV","direction":"UP","points_delta":7,'
           '"driver":"Covered Call","overlay":"COVERED_CALL"}]}')
    payload = RoutineSignalPayload.model_validate_json(raw)
    assert payload.signal_changes[0].overlay == "COVERED_CALL"
    sigs = normalize_payload(payload)
    assert sigs[0].overlay is OverlayType.COVERED_CALL
    assert sigs[0].symbol == "US.CLOV"


def test_no_overlay_is_equity():
    raw = ('{"routine_id":"r1","timestamp":"2026-06-15T13:00:00Z",'
           '"signal_changes":[{"ticker":"AAPL","direction":"UP","points_delta":7}]}')
    sigs = normalize_payload(RoutineSignalPayload.model_validate_json(raw))
    assert sigs[0].overlay is None
