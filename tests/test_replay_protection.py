"""Replay protection (C3): defense in depth against a captured/replayed signal
payload being routed as fresh. Two layers:
  1. Webhook freshness window (autotrader/signals/webhook.py) — rejects any
     payload whose timestamp drifts too far (either direction) from "now".
  2. Inbox routine_id dedup + TTL quarantine (autotrader/signals/inbox.py) —
     catches the file-drop adapter path, which never sees the webhook."""
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from autotrader.signals.inbox import SignalInbox, atomic_write_bytes


def _payload(routine_id="r1", ts=None):
    ts = ts or datetime(2026, 7, 6, 14, 0, tzinfo=timezone.utc)
    return json.dumps({
        "routine_id": routine_id, "timestamp": ts.isoformat(),
        "signal_changes": [{"ticker": "US.TEST", "direction": "UP",
                            "points_delta": 5, "driver": "test"}],
    }).encode()


def test_duplicate_routine_id_processed_once(tmp_path):
    seen = {}
    now = datetime(2026, 7, 6, 14, 5, tzinfo=timezone.utc)
    inbox = SignalInbox(str(tmp_path), seen_get=seen.get, seen_set=seen.__setitem__,
                        now_fn=lambda: now)
    atomic_write_bytes(Path(tmp_path), _payload())
    assert len(inbox.poll()) == 1
    assert seen.get("routine:r1")
    atomic_write_bytes(Path(tmp_path), _payload())      # replay, same routine_id
    assert inbox.poll() == []                           # deduped, no signals


def test_stale_payload_quarantined(tmp_path):
    now = datetime(2026, 7, 8, 14, 0, tzinfo=timezone.utc)   # 2 days later
    inbox = SignalInbox(str(tmp_path), ttl_hours=24.0, now_fn=lambda: now)
    atomic_write_bytes(Path(tmp_path), _payload())
    assert inbox.poll() == []
    assert list((Path(tmp_path) / "rejected").iterdir())     # preserved, not routed


def test_fresh_payload_within_ttl_is_processed(tmp_path):
    # Sanity counterpart to the stale test: same TTL, but "now" is within the
    # window -> the payload is processed normally, not quarantined.
    now = datetime(2026, 7, 6, 20, 0, tzinfo=timezone.utc)   # 6h later
    inbox = SignalInbox(str(tmp_path), ttl_hours=24.0, now_fn=lambda: now)
    atomic_write_bytes(Path(tmp_path), _payload())
    assert len(inbox.poll()) == 1
    assert not list((Path(tmp_path) / "rejected").glob("*.json"))


def test_ttl_disabled_when_zero(tmp_path):
    now = datetime(2027, 1, 1, tzinfo=timezone.utc)   # far future — would be stale at ttl>0
    inbox = SignalInbox(str(tmp_path), ttl_hours=0.0, now_fn=lambda: now)
    atomic_write_bytes(Path(tmp_path), _payload())
    assert len(inbox.poll()) == 1


def test_dedup_disabled_without_seen_get(tmp_path):
    # Without seen_get/seen_set wired, replays of the same routine_id are NOT
    # deduped by the inbox (dedup is opt-in via injected callables).
    now = datetime(2026, 7, 6, 14, 5, tzinfo=timezone.utc)
    inbox = SignalInbox(str(tmp_path), now_fn=lambda: now)
    atomic_write_bytes(Path(tmp_path), _payload())
    assert len(inbox.poll()) == 1
    atomic_write_bytes(Path(tmp_path), _payload())
    assert len(inbox.poll()) == 1   # no dedup store -> processed again


def test_webhook_rejects_outside_freshness_window():
    from autotrader.signals.webhook import create_app
    import hashlib, hmac as hmac_mod, tempfile
    secret = "s3cret"
    with tempfile.TemporaryDirectory() as d:
        now = datetime(2026, 7, 6, 15, 0, tzinfo=timezone.utc)
        app = create_app(d, secret, freshness_minutes=15.0, now_fn=lambda: now)
        c = app.test_client()
        body = _payload(ts=datetime(2026, 7, 6, 14, 0, tzinfo=timezone.utc))  # 60 min old
        sig = "sha256=" + hmac_mod.new(secret.encode(), body, hashlib.sha256).hexdigest()
        r = c.post("/webhook/sweep", data=body,
                   headers={"X-Webhook-Token": secret, "X-Webhook-Signature": sig})
        assert r.status_code == 400
        assert r.get_json()["error"] == "stale_timestamp"
        assert not [p for p in Path(d).glob("*.json")]        # nothing enqueued
        rej = list((Path(d) / "rejected").glob("*.json"))
        assert len(rej) == 1 and rej[0].read_bytes() == body   # preserved for replay/diagnosis


def test_webhook_rejects_future_dated_payload():
    # Freshness is bidirectional: a suspiciously future-dated payload (e.g. a
    # clock-skewed or forged producer) is rejected too, not just stale ones.
    from autotrader.signals.webhook import create_app
    import hashlib, hmac as hmac_mod, tempfile
    secret = "s3cret"
    with tempfile.TemporaryDirectory() as d:
        now = datetime(2026, 7, 6, 15, 0, tzinfo=timezone.utc)
        app = create_app(d, secret, freshness_minutes=15.0, now_fn=lambda: now)
        c = app.test_client()
        body = _payload(ts=datetime(2026, 7, 6, 16, 30, tzinfo=timezone.utc))  # 90 min ahead
        sig = "sha256=" + hmac_mod.new(secret.encode(), body, hashlib.sha256).hexdigest()
        r = c.post("/webhook/sweep", data=body,
                   headers={"X-Webhook-Token": secret, "X-Webhook-Signature": sig})
        assert r.status_code == 400
        assert r.get_json()["error"] == "stale_timestamp"


def test_webhook_accepts_within_freshness_window():
    from autotrader.signals.webhook import create_app
    import hashlib, hmac as hmac_mod, tempfile
    secret = "s3cret"
    with tempfile.TemporaryDirectory() as d:
        now = datetime(2026, 7, 6, 15, 0, tzinfo=timezone.utc)
        app = create_app(d, secret, freshness_minutes=15.0, now_fn=lambda: now)
        c = app.test_client()
        body = _payload(ts=datetime(2026, 7, 6, 14, 50, tzinfo=timezone.utc))  # 10 min old
        sig = "sha256=" + hmac_mod.new(secret.encode(), body, hashlib.sha256).hexdigest()
        r = c.post("/webhook/sweep", data=body,
                   headers={"X-Webhook-Token": secret, "X-Webhook-Signature": sig})
        assert r.status_code == 202
        assert list(Path(d).glob("*.json"))


def test_webhook_freshness_disabled_when_zero():
    from autotrader.signals.webhook import create_app
    import hashlib, hmac as hmac_mod, tempfile
    secret = "s3cret"
    with tempfile.TemporaryDirectory() as d:
        now = datetime(2026, 7, 6, 15, 0, tzinfo=timezone.utc)
        app = create_app(d, secret, freshness_minutes=0, now_fn=lambda: now)
        c = app.test_client()
        # Ancient timestamp -- would fail any positive freshness window.
        body = _payload(ts=datetime(2020, 1, 1, tzinfo=timezone.utc))
        sig = "sha256=" + hmac_mod.new(secret.encode(), body, hashlib.sha256).hexdigest()
        r = c.post("/webhook/sweep", data=body,
                   headers={"X-Webhook-Token": secret, "X-Webhook-Signature": sig})
        assert r.status_code == 202


def test_webhook_freshness_applies_to_coerced_path():
    # A drifted-shape payload that gets coerced onto the canonical schema must
    # still pass through the freshness gate before being enqueued.
    from autotrader.signals.webhook import create_app
    import hashlib, hmac as hmac_mod, tempfile
    secret = "s3cret"
    with tempfile.TemporaryDirectory() as d:
        now = datetime(2026, 7, 6, 15, 0, tzinfo=timezone.utc)
        app = create_app(d, secret, freshness_minutes=15.0, now_fn=lambda: now)
        c = app.test_client()
        drifted = json.dumps({
            "run_id": "routine-20260706-1000-abcdef",   # -> stale ts (5h before "now")
            "sweep_date": "2026-07-06",
            "signal_changes": 1,
            "signals": [{"ticker": "AAPL", "signal": "BUY", "prior_signal": "HOLD",
                        "direction": "upgrade", "points_delta": 8, "changed": True}],
        }).encode()
        sig = "sha256=" + hmac_mod.new(secret.encode(), drifted, hashlib.sha256).hexdigest()
        r = c.post("/webhook/sweep", data=drifted,
                   headers={"X-Webhook-Token": secret, "X-Webhook-Signature": sig})
        assert r.status_code == 400
        assert r.get_json()["error"] == "stale_timestamp"
        assert not list(Path(d).glob("*.json"))


def test_end_to_end_webhook_enqueue_then_inbox_dedup():
    # A payload that clears the webhook's freshness gate is enqueued, and a
    # replayed drop of the SAME routine_id straight into the file-drop inbox
    # (bypassing the webhook entirely) is still caught by inbox-level dedup.
    from autotrader.signals.webhook import create_app
    import hashlib, hmac as hmac_mod, tempfile
    secret = "s3cret"
    with tempfile.TemporaryDirectory() as d:
        now = datetime(2026, 7, 6, 15, 0, tzinfo=timezone.utc)
        app = create_app(d, secret, freshness_minutes=15.0, now_fn=lambda: now)
        c = app.test_client()
        body = _payload(ts=datetime(2026, 7, 6, 14, 50, tzinfo=timezone.utc))
        sig = "sha256=" + hmac_mod.new(secret.encode(), body, hashlib.sha256).hexdigest()
        r = c.post("/webhook/sweep", data=body,
                   headers={"X-Webhook-Token": secret, "X-Webhook-Signature": sig})
        assert r.status_code == 202

        seen = {}
        inbox = SignalInbox(d, seen_get=seen.get, seen_set=seen.__setitem__,
                            now_fn=lambda: now)
        assert len(inbox.poll()) == 1              # the webhook-enqueued file
        # Simulate the file-drop adapter delivering the SAME routine_id directly
        # (bypassing the webhook and its freshness/HMAC checks entirely).
        atomic_write_bytes(Path(d), body)
        assert inbox.poll() == []                   # inbox-level dedup catches it
