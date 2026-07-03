import json
from datetime import datetime, timezone
from pathlib import Path

from autotrader.signals.inbox import SignalInbox, atomic_write_bytes


def _payload(routine_id, ticker="US.TEST", direction="UP", ts="2026-07-06T14:00:00Z",
             targets=None):
    d = {"routine_id": routine_id, "timestamp": ts,
         "signal_changes": [{"ticker": ticker, "direction": direction,
                             "points_delta": 5, "driver": "t"}]}
    if targets is not None:
        d["portfolio_targets"] = targets
    return json.dumps(d).encode()


def test_files_process_in_arrival_order(tmp_path):
    inbox = SignalInbox(str(tmp_path))
    atomic_write_bytes(Path(tmp_path), _payload("r1", direction="UP"))
    atomic_write_bytes(Path(tmp_path), _payload("r2", direction="DOWN"))
    sigs = inbox.poll()
    assert [s.direction for s in sigs] == ["BUY", "SELL"]   # arrival order, always


def test_future_dated_targets_clamped_to_today(tmp_path):
    recorded = []
    now = datetime(2026, 7, 6, 14, 0, tzinfo=timezone.utc)
    inbox = SignalInbox(str(tmp_path), now_fn=lambda: now,
                        on_targets=lambda d, t: recorded.append(d))
    atomic_write_bytes(Path(tmp_path), _payload(
        "r3", ts="2027-01-01T14:00:00Z",
        targets=[{"symbol": "US.TEST", "score": 1.0}]))
    inbox.poll()
    assert recorded == ["2026-07-06"]          # clamped, not 2027-01-01


def test_failing_targets_sink_does_not_block_signals(tmp_path):
    def boom(d, t):
        import sqlite3
        raise sqlite3.OperationalError("database is locked")
    inbox = SignalInbox(str(tmp_path), on_targets=boom)
    atomic_write_bytes(Path(tmp_path), _payload(
        "r4", targets=[{"symbol": "US.TEST", "score": 1.0}]))
    sigs = inbox.poll()
    assert len(sigs) == 1                       # signals still routed
    assert not list(Path(tmp_path).glob("*.json"))   # file moved out of inbox
