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


def test_drops_overlay_intended_change_missing_overlay_enum():
    # driver names an overlay + HEDGE transition, but no overlay enum -> dropped.
    payload = RoutineSignalPayload(
        routine_id="r", timestamp="2026-06-19T13:20:01",
        signal_changes=[_change(ticker="US.DIVO", direction="DOWN",
                                transition=["LONG", "HEDGE"], points_delta=-6,
                                driver="Collar (options overlay)")])
    assert normalize_payload(payload) == []


def test_keeps_overlay_change_when_enum_present():
    payload = RoutineSignalPayload(
        routine_id="r", timestamp="2026-06-19T13:20:01",
        signal_changes=[_change(ticker="US.DIVO", direction="DOWN",
                                transition=["LONG", "HEDGE"], points_delta=-6,
                                driver="Collar (options overlay)", overlay="COLLAR")])
    sigs = normalize_payload(payload)
    assert len(sigs) == 1
    assert sigs[0].symbol == "US.DIVO" and sigs[0].overlay is not None


def test_transition_marker_alone_trips_guard():
    # driver has no "overlay" marker, but transition target HEDGE does.
    payload = RoutineSignalPayload(
        routine_id="r", timestamp="2026-06-19T13:20:01",
        signal_changes=[_change(ticker="US.SPCE", direction="DOWN",
                                transition=["LONG", "HEDGE"], points_delta=-6,
                                driver="")])
    assert normalize_payload(payload) == []


def test_driver_marker_alone_trips_guard():
    # transition target is not an overlay state, but driver names an overlay.
    payload = RoutineSignalPayload(
        routine_id="r", timestamp="2026-06-19T13:20:01",
        signal_changes=[_change(ticker="US.O", direction="UP",
                                transition=["LONG", "SOMETHING"], points_delta=3,
                                driver="Covered Call (options overlay)")])
    assert normalize_payload(payload) == []


def test_up_income_overlay_intent_missing_enum_is_dropped():
    payload = RoutineSignalPayload(
        routine_id="r", timestamp="2026-06-19T13:20:01",
        signal_changes=[_change(ticker="US.IBIT", direction="UP",
                                transition=["LONG", "INCOME"], points_delta=3,
                                driver="Poor Man's Covered Call (options overlay)")])
    assert normalize_payload(payload) == []


def test_genuine_plain_buy_is_not_dropped():
    payload = RoutineSignalPayload(
        routine_id="r", timestamp="2026-06-19T13:20:01",
        signal_changes=[_change(ticker="US.DIVO", direction="UP",
                                transition=["HOLD", "BUY"], points_delta=7, driver="")])
    sigs = normalize_payload(payload)
    assert len(sigs) == 1 and sigs[0].direction == "BUY" and sigs[0].overlay is None


def test_genuine_plain_sell_exit_is_not_dropped():
    # A real exit (transition target not an overlay state, no overlay driver) still sells.
    payload = RoutineSignalPayload(
        routine_id="r", timestamp="2026-06-19T13:20:01",
        signal_changes=[_change(ticker="US.YNVDA", direction="DOWN",
                                transition=["CAUTION", "AVOID"], points_delta=-17,
                                driver="ticker sweep")])
    sigs = normalize_payload(payload)
    assert len(sigs) == 1 and sigs[0].direction == "SELL"


def test_empty_transition_no_driver_is_not_overlay_intended():
    payload = RoutineSignalPayload(
        routine_id="r", timestamp="2026-06-19T13:20:01",
        signal_changes=[_change(ticker="US.AAPL", direction="UP",
                                transition=[], points_delta=5, driver="")])
    assert len(normalize_payload(payload)) == 1


def test_mixed_payload_drops_only_malformed_overlay_change():
    payload = RoutineSignalPayload(
        routine_id="r", timestamp="2026-06-19T13:20:01",
        signal_changes=[
            _change(ticker="US.DIVO", direction="DOWN", transition=["LONG", "HEDGE"],
                    points_delta=-6, driver="Collar (options overlay)"),        # dropped
            _change(ticker="US.AAPL", direction="UP", transition=["HOLD", "BUY"],
                    points_delta=7, driver=""),                                  # kept
        ])
    sigs = normalize_payload(payload)
    assert [s.symbol for s in sigs] == ["US.AAPL"]
