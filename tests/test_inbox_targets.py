from autotrader.signals.inbox import SignalInbox, atomic_write_bytes


def test_on_targets_called_with_extracted_rows(tmp_path):
    captured = []
    inbox = SignalInbox(str(tmp_path),
                        on_targets=lambda as_of, rows: captured.append((as_of, rows)))
    payload = (
        '{"routine_id":"r1","timestamp":"2026-06-16T12:00:00Z",'
        '"signal_changes":[],'
        '"portfolio_targets":[{"symbol":"US.AAPL","score":80.0},'
        '{"symbol":"US.MSFT","score":60.0}]}'
    )
    atomic_write_bytes(tmp_path, payload.encode())
    inbox.poll()
    assert captured == [("2026-06-16", [("US.AAPL", 80.0), ("US.MSFT", 60.0)])]


def test_no_targets_does_not_call_callback(tmp_path):
    captured = []
    inbox = SignalInbox(str(tmp_path),
                        on_targets=lambda as_of, rows: captured.append(rows))
    payload = ('{"routine_id":"r1","timestamp":"2026-06-16T12:00:00Z",'
               '"signal_changes":[]}')
    atomic_write_bytes(tmp_path, payload.encode())
    inbox.poll()
    assert captured == []
