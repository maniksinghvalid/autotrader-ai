"""normalize_payload turns a validated RoutineSignalPayload into domain.Signal
values: UP->BUY / DOWN->SELL, ticker->US.TICKER, points_delta->confidence."""
import pytest
from pydantic import ValidationError

from autotrader.signals.schema import RoutineSignalPayload, SignalChange
from autotrader.signals.normalize import normalize_change, normalize_payload


def _change(**over):
    base = dict(ticker="AAPL", direction="UP", transition=["50", "200"],
                points_delta=10, driver="breakout")
    base.update(over)
    return SignalChange(**base)


def test_payload_normalizes_each_change_to_signal():
    payload = RoutineSignalPayload(
        routine_id="r1", timestamp="2026-06-12T09:46:00-04:00",
        signal_changes=[_change(ticker="AAPL", direction="UP"),
                        _change(ticker="MSFT", direction="DOWN")])
    sigs = normalize_payload(payload)
    assert [s.symbol for s in sigs] == ["US.AAPL", "US.MSFT"]
    assert [s.direction for s in sigs] == ["BUY", "SELL"]


def test_qualified_symbol_passes_through_uppercased():
    sig = normalize_change(_change(ticker="us.nio"))
    assert sig.symbol == "US.NIO"


def test_confidence_scales_and_clamps_to_one():
    assert normalize_change(_change(points_delta=5)).confidence == pytest.approx(0.5)
    assert normalize_change(_change(points_delta=50)).confidence == 1.0   # clamped
    assert normalize_change(_change(points_delta=0)).confidence == 0.0    # drops at filter


def test_custom_confidence_scale_overrides_default():
    sig = normalize_change(_change(points_delta=5), confidence_scale=5.0)
    assert sig.confidence == 1.0


def test_invalid_direction_is_rejected_by_schema():
    with pytest.raises(ValidationError):
        SignalChange(ticker="AAPL", direction="SIDEWAYS", transition=[],
                     points_delta=1, driver="x")
