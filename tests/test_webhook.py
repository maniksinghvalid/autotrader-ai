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


def _client(tmp_path, max_body=65536):
    app = create_app(str(tmp_path / "inbox"), _SECRET, max_body=max_body)
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
    sigs = SignalInbox(str(inbox_path)).poll()      # the trader's consumer
    assert len(sigs) == 1
    assert sigs[0].symbol == "US.AAPL" and sigs[0].direction == "BUY"


def test_webhook_module_does_not_import_moomoo_sdk():
    code = ("import importlib, sys\n"
            "importlib.import_module('autotrader.signals.webhook')\n"
            "assert 'moomoo' not in sys.modules, 'webhook pulled in the SDK'\n"
            "print('ok')\n")
    res = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert res.returncode == 0 and "ok" in res.stdout, res.stderr
