"""SignalInbox: file-drop ingress. poll() validates+normalizes *.json payloads,
moving each to processed/ (ok) or rejected/ (bad). A malformed file never crashes
the loop, and a consumed file is never re-processed."""
import json

from autotrader.signals.inbox import SignalInbox

_PAYLOAD = {
    "routine_id": "r1", "timestamp": "2026-06-12T09:46:00-04:00",
    "signal_changes": [
        {"ticker": "AAPL", "direction": "UP", "transition": ["50", "200"],
         "points_delta": 10, "driver": "breakout"}
    ],
}


def test_poll_reads_valid_file_and_moves_to_processed(tmp_path):
    inbox = SignalInbox(str(tmp_path), ttl_hours=0)
    (tmp_path / "a.json").write_text(json.dumps(_PAYLOAD))
    sigs = inbox.poll()
    assert len(sigs) == 1
    assert sigs[0].symbol == "US.AAPL" and sigs[0].direction == "BUY"
    assert not (tmp_path / "a.json").exists()
    assert (tmp_path / "processed" / "a.json").exists()


def test_poll_moves_malformed_file_to_rejected_without_crashing(tmp_path):
    inbox = SignalInbox(str(tmp_path), ttl_hours=0)
    (tmp_path / "bad.json").write_text("{not valid json")
    sigs = inbox.poll()
    assert sigs == []
    assert (tmp_path / "rejected" / "bad.json").exists()


def test_poll_empty_dir_returns_empty(tmp_path):
    inbox = SignalInbox(str(tmp_path), ttl_hours=0)
    assert inbox.poll() == []


def test_poll_is_idempotent_across_runs(tmp_path):
    inbox = SignalInbox(str(tmp_path), ttl_hours=0)
    (tmp_path / "a.json").write_text(json.dumps(_PAYLOAD))
    assert len(inbox.poll()) == 1
    assert inbox.poll() == []   # file already consumed -> nothing re-processed
