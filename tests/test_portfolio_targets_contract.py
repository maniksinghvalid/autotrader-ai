"""Cross-repo contract: a sweep payload shaped like build_sweep_payload.py's
output (ai-trading-claude) must survive the webhook coerce + schema validation
with its portfolio_targets intact. Guards the two repos against silent drift."""
import json

from autotrader.signals.coerce import coerce_payload
from autotrader.signals.schema import RoutineSignalPayload


def test_builder_sweep_payload_targets_survive_coerce():
    raw = json.dumps({
        "routine_id": "routine-20260622-1455-abc123",
        "timestamp": "2026-06-22T14:55:00",
        "signal_changes": [{"ticker": "US.DIVO", "direction": "UP",
                            "transition": ["HOLD", "BUY"], "points_delta": 7,
                            "driver": "ticker sweep (score 71)"}],
        "hard_stops": {},
        "catalysts": [],
        "portfolio_targets": [{"symbol": "US.DIVO", "score": 71.0},
                              {"symbol": "US.O", "score": 55.0}],
    }).encode()

    out = coerce_payload(raw)
    assert out is not None, "coerce rejected a well-formed builder payload"

    payload = RoutineSignalPayload.model_validate_json(out)
    assert [(t.symbol, t.score) for t in payload.portfolio_targets] == \
           [("US.DIVO", 71.0), ("US.O", 55.0)]


def test_builder_sweep_payload_without_targets_is_empty():
    raw = json.dumps({
        "routine_id": "routine-20260622-1455-abc123",
        "timestamp": "2026-06-22T14:55:00",
        "signal_changes": [],
        "hard_stops": {},
        "catalysts": [],
        "portfolio_targets": [],
    }).encode()

    out = coerce_payload(raw)
    assert out is not None
    payload = RoutineSignalPayload.model_validate_json(out)
    assert payload.portfolio_targets == []
