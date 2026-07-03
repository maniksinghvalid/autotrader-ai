"""Webhook ingress: authenticated (shared secret + HMAC over raw body),
enqueue-only. Valid payloads are atomically written into the inbox dir and are
consumed unchanged by SignalInbox. The module never imports the moomoo SDK."""
import hmac, hashlib, json, subprocess, sys
import pytest

pytest.importorskip("flask")  # webhook is an optional extra

from autotrader.signals.webhook import create_app
from autotrader.signals.inbox import SignalInbox

_SECRET = "test-secret-123"
_BODY = json.dumps({
    "routine_id": "r1", "timestamp": "2026-06-13T09:46:00-04:00",
    "signal_changes": [
        {"ticker": "AAPL", "direction": "UP", "transition": ["50", "200"],
         "points_delta": 10, "driver": "breakout"}
    ],
}).encode()


def _sig(secret: bytes, body: bytes) -> str:
    return "sha256=" + hmac.new(secret, body, hashlib.sha256).hexdigest()


def _client(tmp_path, max_body=65536, freshness_minutes=0, now_fn=None):
    # freshness_minutes=0 disables the C3 freshness window by default here: these
    # tests use fixed historical timestamps and are not testing replay protection
    # (that's covered by tests/test_replay_protection.py).
    app = create_app(str(tmp_path / "inbox"), _SECRET, max_body=max_body,
                     freshness_minutes=freshness_minutes, now_fn=now_fn)
    app.config.update(TESTING=True)
    return app.test_client(), tmp_path / "inbox"


def _headers(body=_BODY, secret=_SECRET):
    return {"X-Webhook-Token": secret,
            "X-Webhook-Signature": _sig(secret.encode(), body),
            "Content-Type": "application/json"}


def test_healthz_needs_no_auth(tmp_path):
    c, _ = _client(tmp_path)
    assert c.get("/healthz").status_code == 200


def test_missing_token_is_401_and_writes_nothing(tmp_path):
    c, inbox = _client(tmp_path)
    r = c.post("/webhook/sweep", data=_BODY, content_type="application/json")
    assert r.status_code == 401
    assert list(inbox.glob("*.json")) == []


def test_wrong_token_is_401(tmp_path):
    c, _ = _client(tmp_path)
    h = _headers(); h["X-Webhook-Token"] = "nope"
    assert c.post("/webhook/sweep", data=_BODY, headers=h).status_code == 401


def test_bad_hmac_signature_is_401(tmp_path):
    c, _ = _client(tmp_path)
    h = _headers(); h["X-Webhook-Signature"] = _sig(b"wrong-key", _BODY)
    assert c.post("/webhook/sweep", data=_BODY, headers=h).status_code == 401


def _audit_lines(inbox):
    p = inbox / "audit" / "webhook-auth.jsonl"
    return [json.loads(l) for l in p.read_text().splitlines() if l.strip()] if p.exists() else []


def test_missing_token_audit_reason_and_caller_metadata(tmp_path):
    c, inbox = _client(tmp_path)
    r = c.post("/webhook/sweep", data=_BODY, content_type="application/json",
               headers={"User-Agent": "PostmanRuntime/7.37", "X-Forwarded-For": "203.0.113.9"})
    assert r.status_code == 401
    a = _audit_lines(inbox)
    assert len(a) == 1
    assert a[0]["reason"] == "missing_token"
    assert a[0]["token_present"] is False
    assert a[0]["user_agent"].startswith("PostmanRuntime")   # tells Postman from the routine
    assert a[0]["forwarded_for"] == "203.0.113.9"            # real caller behind ngrok


def test_wrong_token_audit_reason_never_logs_secret(tmp_path):
    c, inbox = _client(tmp_path)
    h = _headers(); h["X-Webhook-Token"] = "nope"
    assert c.post("/webhook/sweep", data=_BODY, headers=h).status_code == 401
    a = _audit_lines(inbox)[0]
    assert a["reason"] == "token_mismatch"
    assert a["token_present"] is True and a["signature_present"] is True
    blob = json.dumps(a)
    assert "nope" not in blob and _SECRET not in blob       # provided/real secret never persisted


def test_bad_signature_audit_reason(tmp_path):
    c, inbox = _client(tmp_path)
    h = _headers(); h["X-Webhook-Signature"] = _sig(b"wrong-key", _BODY)
    assert c.post("/webhook/sweep", data=_BODY, headers=h).status_code == 401
    assert _audit_lines(inbox)[0]["reason"] == "bad_signature"


def test_valid_request_writes_no_auth_audit(tmp_path):
    c, inbox = _client(tmp_path)
    assert c.post("/webhook/sweep", data=_BODY, headers=_headers()).status_code == 202
    assert _audit_lines(inbox) == []


def test_auth_audit_not_consumed_by_inbox(tmp_path):
    c, inbox_path = _client(tmp_path)
    c.post("/webhook/sweep", data=_BODY, content_type="application/json")  # 401 -> audit
    assert _audit_lines(inbox_path)                       # audit line written
    assert SignalInbox(str(inbox_path), ttl_hours=0).poll() == []      # but the trader never consumes it


def test_valid_request_enqueues_and_returns_202(tmp_path):
    c, inbox = _client(tmp_path)
    r = c.post("/webhook/sweep", data=_BODY, headers=_headers())
    assert r.status_code == 202
    assert r.get_json()["routine_id"] == "r1"
    files = list(inbox.glob("*.json"))
    assert len(files) == 1
    assert files[0].read_bytes() == _BODY          # written verbatim
    assert not list(inbox.glob("*.part"))          # no partial temp left behind


def test_invalid_schema_is_400_with_detail_and_quarantined(tmp_path):
    c, inbox = _client(tmp_path)
    bad = json.dumps({"routine_id": "r1", "timestamp": "2026-06-13T09:46:00-04:00",
                      "signal_changes": [{"ticker": "AAPL", "direction": "SIDEWAYS",
                                          "points_delta": 1}]}).encode()
    r = c.post("/webhook/sweep", data=bad, headers=_headers(body=bad))
    assert r.status_code == 400
    body = r.get_json()
    assert body["error"] == "invalid payload"
    # field-level detail names the offending location
    locs = " ".join(d["loc"] for d in body["detail"])
    assert "direction" in locs
    # nothing enqueued for the trader, but the body is preserved for replay
    assert list(inbox.glob("*.json")) == []
    rej = list((inbox / "rejected").glob("*.json"))
    assert len(rej) == 1 and rej[0].read_bytes() == bad
    assert not list((inbox / "rejected").glob("*.part"))


def test_malformed_json_is_400_with_detail_and_quarantined(tmp_path):
    c, inbox = _client(tmp_path)
    bad = b"{not valid json"
    r = c.post("/webhook/sweep", data=bad, headers=_headers(body=bad))
    assert r.status_code == 400
    assert r.get_json()["error"] == "invalid payload"
    assert list(inbox.glob("*.json")) == []
    rej = list((inbox / "rejected").glob("*.json"))
    assert len(rej) == 1 and rej[0].read_bytes() == bad


def test_oversized_body_is_413(tmp_path):
    c, inbox = _client(tmp_path, max_body=64)   # tiny cap
    big = json.dumps({"routine_id": "r1", "timestamp": "2026-06-13T09:46:00-04:00",
                      "signal_changes": [{"ticker": "AAPL", "direction": "UP",
                                          "transition": [], "points_delta": 1,
                                          "driver": "x"}]}).encode()
    r = c.post("/webhook/sweep", data=big, headers=_headers(body=big))
    assert r.status_code == 413
    assert list(inbox.glob("*.json")) == []


def test_enqueued_file_is_consumed_by_inbox_end_to_end(tmp_path):
    c, inbox_path = _client(tmp_path)
    c.post("/webhook/sweep", data=_BODY, headers=_headers())
    sigs = SignalInbox(str(inbox_path), ttl_hours=0).poll()      # the trader's consumer
    assert len(sigs) == 1
    assert sigs[0].symbol == "US.AAPL" and sigs[0].direction == "BUY"


def test_drifted_payload_is_coerced_enqueued_and_consumed(tmp_path):
    # The shape the daily-sweep agent emitted (run_id/sweep_date/signals + an int
    # signal_changes) is accepted (202, coerced), enqueued as CANONICAL bytes, and
    # consumed by the same strict inbox the trader uses.
    drifted = json.dumps({
        "run_id": "routine-20260617-1637-4fea27",
        "sweep_date": "2026-06-17",
        "signal_changes": 1,
        "signals": [
            {"ticker": "XEQT", "signal": "BUY", "prior_signal": "BUY", "changed": False},
            {"ticker": "AAPL", "signal": "BUY", "prior_signal": "HOLD",
             "direction": "upgrade", "points_delta": 8, "changed": True},
        ],
    }).encode()
    c, inbox_path = _client(tmp_path)
    r = c.post("/webhook/sweep", data=drifted, headers=_headers(body=drifted))
    assert r.status_code == 202
    body = r.get_json()
    assert body["coerced"] is True and body["signals"] == 1   # unchanged XEQT dropped
    # enqueued file is canonical (re-validates) and the trader consumes it
    sigs = SignalInbox(str(inbox_path), ttl_hours=0).poll()
    assert len(sigs) == 1
    assert sigs[0].symbol == "US.AAPL" and sigs[0].direction == "BUY"


def test_unsalvageable_payload_still_400s(tmp_path):
    c, inbox = _client(tmp_path)
    bad = json.dumps({"run_id": "r", "sweep_date": "2026-06-17",
                      "signals": [{"ticker": "AAPL", "direction": "SIDEWAYS"}]}).encode()
    r = c.post("/webhook/sweep", data=bad, headers=_headers(body=bad))
    assert r.status_code == 400
    assert list(inbox.glob("*.json")) == []
    assert len(list((inbox / "rejected").glob("*.json"))) == 1


def _client_v6a(tmp_path, secret=_SECRET, token=None, max_body=65536,
                audit_max_bytes=10_000_000, freshness_minutes=0, now_fn=None):
    # freshness_minutes=0 disables C3's freshness window by default (see _client).
    app = create_app(str(tmp_path / "inbox"), secret, max_body=max_body,
                     token=token, audit_max_bytes=audit_max_bytes,
                     freshness_minutes=freshness_minutes, now_fn=now_fn)
    app.config.update(TESTING=True)
    return app.test_client(), tmp_path / "inbox"


def test_non_ascii_token_is_401_not_500(tmp_path):
    # Flask/WSGI decodes headers latin-1; hmac.compare_digest(str, str) raises
    # TypeError on non-ASCII str, which today crashes with an unhandled 500
    # before the audit write happens. A non-ASCII header must be a clean,
    # audited 401 instead (V6a).
    c, inbox = _client_v6a(tmp_path)
    r = c.post("/webhook/sweep", data=b"{}",
               headers={"X-Webhook-Token": "café", "X-Webhook-Signature": "x"})
    assert r.status_code == 401
    audit = inbox / "audit" / "webhook-auth.jsonl"
    assert audit.exists() and "token_mismatch" in audit.read_text()


def test_dual_secret_token_alone_cannot_forge(tmp_path):
    # The bearer token (X-Webhook-Token) must be independent of the HMAC signing
    # key: knowing the token alone (e.g. from ngrok's request inspector) must
    # NOT be enough to forge a valid signature (V6a).
    c, inbox = _client_v6a(tmp_path, secret="signing-key", token="bearer-token")
    body = b'{"routine_id":"r1","timestamp":"2026-07-06T14:00:00Z","signal_changes":[]}'
    # correct bearer token, but signature made with the TOKEN not the signing key
    r = c.post("/webhook/sweep", data=body, headers={
        "X-Webhook-Token": "bearer-token",
        "X-Webhook-Signature": _sig(b"bearer-token", body)})
    assert r.status_code == 401
    r2 = c.post("/webhook/sweep", data=body, headers={
        "X-Webhook-Token": "bearer-token",
        "X-Webhook-Signature": _sig(b"signing-key", body)})
    assert r2.status_code == 202


def test_single_secret_mode_still_works_back_compat(tmp_path):
    # token=None (or omitted) falls back to secret == token, matching pre-V6a
    # callers that only pass (inbox_dir, secret).
    c, inbox = _client_v6a(tmp_path)  # token defaults to None -> falls back to _SECRET
    assert c.post("/webhook/sweep", data=_BODY, headers=_headers()).status_code == 202


def test_audit_writes_are_bounded(tmp_path):
    # Anonymous hammering from one address must not grow the audit file without
    # bound: truncate attacker-controlled fields and throttle writes per-address
    # (V6a).
    c, inbox = _client_v6a(tmp_path)
    for _ in range(50):   # anonymous hammering from one address
        c.post("/webhook/sweep", data=b"x", headers={"User-Agent": "A" * 10000})
    lines = (inbox / "audit" / "webhook-auth.jsonl").read_text().splitlines()
    assert len(lines) <= 10                      # per-IP throttle
    assert all(len(json.loads(l).get("user_agent") or "") <= 256 for l in lines)


def test_audit_write_skipped_once_file_exceeds_max_bytes(tmp_path):
    c, inbox = _client_v6a(tmp_path, audit_max_bytes=10)  # tiny cap
    c.post("/webhook/sweep", data=b"x")  # first write goes through (file didn't exist yet)
    c.post("/webhook/sweep", data=b"x")  # file now over cap -> second write skipped
    lines = (inbox / "audit" / "webhook-auth.jsonl").read_text().splitlines()
    assert len(lines) == 1


def test_webhook_module_does_not_import_moomoo_sdk():
    code = ("import importlib, sys\n"
            "importlib.import_module('autotrader.signals.webhook')\n"
            "assert 'moomoo' not in sys.modules, 'webhook pulled in the SDK'\n"
            "print('ok')\n")
    res = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert res.returncode == 0 and "ok" in res.stdout, res.stderr
