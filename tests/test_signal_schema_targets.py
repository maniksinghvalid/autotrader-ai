from autotrader.signals.schema import RoutineSignalPayload, TargetWeight


def test_payload_without_targets_defaults_empty():
    p = RoutineSignalPayload.model_validate({
        "routine_id": "r1", "timestamp": "2026-06-16T12:00:00Z",
        "signal_changes": [],
    })
    assert p.portfolio_targets == []


def test_payload_parses_portfolio_targets():
    p = RoutineSignalPayload.model_validate({
        "routine_id": "r1", "timestamp": "2026-06-16T12:00:00Z",
        "signal_changes": [],
        "portfolio_targets": [
            {"symbol": "US.AAPL", "score": 80.0},
            {"symbol": "US.MSFT", "score": 60.0},
        ],
    })
    assert [t.symbol for t in p.portfolio_targets] == ["US.AAPL", "US.MSFT"]
    assert isinstance(p.portfolio_targets[0], TargetWeight)
    assert p.portfolio_targets[1].score == 60.0
