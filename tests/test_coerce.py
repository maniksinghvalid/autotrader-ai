"""Lenient coercion of drifted signal payloads onto the canonical schema.

These mirror the real shapes the cloud routine's agent has emitted into
#portfolio-updates when it bypassed its deterministic builder. A coerced body
must re-validate as a strict RoutineSignalPayload (that is the receiver's only
enqueue guarantee), so each test validates the coerced bytes."""
import json

from autotrader.signals.coerce import coerce_payload
from autotrader.signals.schema import RoutineSignalPayload


def _validate(raw: bytes) -> RoutineSignalPayload:
    return RoutineSignalPayload.model_validate_json(raw)


def test_run1637_shape_is_coerced_to_one_change():
    # run_id->routine_id, sweep_date->timestamp, signal_changes is an int count,
    # the array lives under "signals", and only the changed row is directional.
    raw = json.dumps({
        "run_id": "routine-20260617-1637-4fea27",
        "sweep_date": "2026-06-17",
        "signal_changes": 1,
        "signals": [
            {"ticker": "XEQT", "signal": "BUY", "prior_signal": "BUY",
             "score": 73, "changed": False},
            {"ticker": "YNVDA", "signal": "NEUTRAL", "prior_signal": "HOLD",
             "score": 45, "changed": True, "direction": "downgrade",
             "catalyst": "NVIDIA earnings August 2026"},
        ],
    }).encode()
    out = coerce_payload(raw)
    assert out is not None
    p = _validate(out)
    assert p.routine_id == "routine-20260617-1637-4fea27"
    assert len(p.signal_changes) == 1                       # unchanged XEQT dropped
    c = p.signal_changes[0]
    assert c.ticker == "YNVDA" and c.direction == "DOWN"
    assert c.transition == ["HOLD", "NEUTRAL"]
    assert "NVIDIA" in c.driver


def test_prior_label_shape_infers_direction_from_signal_rank():
    # items carry only {ticker, signal, score, prior}: no explicit direction.
    raw = json.dumps({
        "run_id": "r1", "sweep_date": "2026-06-17", "ticker_count": 3,
        "signature": "deadbeef",
        "signals": [
            {"ticker": "DIVO", "signal": "HOLD", "score": 68, "prior": "BUY"},
            {"ticker": "XEQT", "signal": "BUY", "score": 73, "prior": "HOLD"},
            {"ticker": "IAU", "signal": "HOLD", "score": 65, "prior": "HOLD"},
        ],
    }).encode()
    p = _validate(coerce_payload(raw))
    by = {c.ticker: c for c in p.signal_changes}
    assert set(by) == {"DIVO", "XEQT"}                      # unchanged IAU dropped
    assert by["DIVO"].direction == "DOWN"                   # BUY -> HOLD
    assert by["XEQT"].direction == "UP"                     # HOLD -> BUY


def test_explicit_points_delta_and_aliases():
    raw = json.dumps({
        "run_id": "r2", "generated_at": "2026-06-16T12:15:00Z",
        "signal_changes": [
            {"ticker": "SCHF", "prior_signal": "HOLD", "new_signal": "BUY",
             "direction": "UPGRADE", "points_delta": 7,
             "prior_score": 64, "new_score": 71, "qualified_symbol": "SCHF"},
        ],
    }).encode()
    p = _validate(coerce_payload(raw))
    assert len(p.signal_changes) == 1
    c = p.signal_changes[0]
    assert c.ticker == "SCHF" and c.direction == "UP" and c.points_delta == 7


def test_explicit_unknown_direction_is_unsalvageable():
    # "SIDEWAYS" is an explicit but unrecognized direction -> refuse to guess.
    raw = json.dumps({
        "run_id": "r3", "sweep_date": "2026-06-17",
        "signal_changes": [{"ticker": "AAPL", "direction": "SIDEWAYS",
                            "points_delta": 1}],
    }).encode()
    assert coerce_payload(raw) is None


def test_missing_routine_id_is_rejected():
    raw = json.dumps({"sweep_date": "2026-06-17", "signals": []}).encode()
    assert coerce_payload(raw) is None


def test_missing_timestamp_is_rejected():
    raw = json.dumps({"run_id": "r4", "signals": [
        {"ticker": "AAPL", "direction": "UP", "points_delta": 7}]}).encode()
    assert coerce_payload(raw) is None


def test_malformed_json_is_rejected():
    assert coerce_payload(b"{not valid json") is None
    assert coerce_payload(b"[1,2,3]") is None              # not an object


def test_genuine_no_change_sweep_is_accepted_empty():
    raw = json.dumps({
        "run_id": "r5", "sweep_date": "2026-06-17",
        "signals": [{"ticker": "X", "signal": "HOLD", "prior_signal": "HOLD",
                     "changed": False}],
    }).encode()
    out = coerce_payload(raw)
    assert out is not None
    p = _validate(out)
    assert p.signal_changes == []                          # empty is legitimate


def test_hard_stops_and_catalysts_pass_through():
    raw = json.dumps({
        "run_id": "r6", "sweep_date": "2026-06-17",
        "signals": [{"ticker": "SPCE", "direction": "down", "prior_signal": "HOLD",
                     "new_signal": "CAUTION", "new_score": 26, "prior_score": 32}],
        "hard_stops": {"SPCE": 1.0},
        "catalysts": [{"ticker": "SPCE", "event": "delta", "date": "2026-07-01",
                       "value": 26.0}],
    }).encode()
    p = _validate(coerce_payload(raw))
    assert p.hard_stops == {"SPCE": 1.0}
    assert len(p.catalysts) == 1 and p.catalysts[0].ticker == "SPCE"
    assert p.signal_changes[0].direction == "DOWN" and p.signal_changes[0].points_delta == -6
