"""Authenticated, enqueue-only webhook ingress — the ONLY internet-reachable
surface, intended to sit behind ngrok. This module has NO moomoo import and NO
broker handle: it authenticates (shared-secret header + HMAC-SHA256 of the raw
body, constant-time), validates the body as the 2c RoutineSignalPayload, and
ATOMICALLY writes it into the file-drop inbox. The local trader (autotrader.main)
consumes the inbox through the deterministic risk core; OpenD is never exposed
and the order path stays local (research H1 mitigation, research C4)."""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from flask import Flask, jsonify, request
from pydantic import ValidationError

from autotrader.signals.coerce import coerce_payload
from autotrader.signals.schema import RoutineSignalPayload

logger = logging.getLogger("autotrader.signals.webhook")

_MAX_BODY_DEFAULT = 65536
# 401s are written here (NOT the inbox root, so SignalInbox.poll() never consumes it).
_AUTH_AUDIT_SUBDIR = "audit"
_AUTH_AUDIT_FILE = "webhook-auth.jsonl"


def _auth_failure_reason(secret: str, raw: bytes, token: Optional[str],
                         signature: Optional[str]) -> Optional[str]:
    """Return None when authenticated, else a short category naming which check
    failed (for the audit log). Same constant-time comparisons as _verify; the
    category does not leak more than the eventual 401 already does."""
    if not secret:
        return "server_secret_unset"
    if not token:
        return "missing_token"
    if not signature:
        return "missing_signature"
    if not hmac.compare_digest(token, secret):
        return "token_mismatch"
    expected = "sha256=" + hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(signature, expected):
        return "bad_signature"
    return None


def _verify(secret: str, raw: bytes, token: Optional[str], signature: Optional[str]) -> bool:
    """Constant-time check: token must equal the secret AND signature must equal
    'sha256=' + HMAC-SHA256(secret, raw_body). Empty secret => always reject."""
    return _auth_failure_reason(secret, raw, token, signature) is None


def _audit_auth_failure(inbox_dir: Path, record: dict) -> None:
    """Append one JSONL line (reason + caller metadata) for a rejected request to
    <inbox>/audit/webhook-auth.jsonl. Best-effort: an audit failure must NEVER
    break the security response, so errors are swallowed (logged only). Records
    who/why — never the secret or the provided token/signature values."""
    try:
        audit_dir = inbox_dir / _AUTH_AUDIT_SUBDIR
        audit_dir.mkdir(parents=True, exist_ok=True)
        line = json.dumps(record, separators=(",", ":"), ensure_ascii=False)
        with open(audit_dir / _AUTH_AUDIT_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception as e:  # pragma: no cover — defensive; observability must not 500
        logger.error("failed to write webhook auth audit: %s", e)


def _atomic_write(dest_dir: Path, raw: bytes, prefix: str) -> Path:
    """Write raw bytes to a unique <prefix><uuid>.json via temp(.part)->rename, so a
    concurrent reader globbing top-level *.json never sees a partial file."""
    dest_dir.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(dest_dir), prefix="." + prefix, suffix=".json.part")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(raw)
            f.flush()
            os.fsync(f.fileno())
    except Exception:
        os.unlink(tmp)
        raise
    final = dest_dir / f"{prefix}{uuid.uuid4().hex}.json"
    os.replace(tmp, final)  # atomic on POSIX
    return final


def _atomic_enqueue(inbox_dir: Path, raw: bytes) -> Path:
    """Enqueue a VALID body into the inbox root for SignalInbox.poll() to consume."""
    return _atomic_write(inbox_dir, raw, "wh-")


def _quarantine(inbox_dir: Path, raw: bytes) -> Path:
    """Preserve a REJECTED body in <inbox>/rejected/ (mirrors SignalInbox's rejected/
    pattern). poll() globs only the inbox root, so a quarantined file is never consumed —
    it's kept solely so a malformed payload is inspectable and replayable, never lost."""
    return _atomic_write(inbox_dir / "rejected", raw, "rej-")


def create_app(inbox_dir: str, secret: str, max_body: int = _MAX_BODY_DEFAULT) -> Flask:
    app = Flask("autotrader-webhook")
    app.config["MAX_CONTENT_LENGTH"] = max_body  # Flask returns 413 past this
    inbox = Path(os.path.expanduser(inbox_dir))

    @app.get("/healthz")
    def healthz():
        return jsonify({"status": "ok"}), 200

    @app.post("/webhook/sweep")
    def sweep():
        raw = request.get_data(cache=False)  # raw bytes — HMAC must match exactly
        token = request.headers.get("X-Webhook-Token")
        signature = request.headers.get("X-Webhook-Signature")
        reason = _auth_failure_reason(secret, raw, token, signature)
        if reason is not None:
            # Persist who/why so a 401 is diagnosable from disk (terminal logs are
            # ephemeral). forwarded_for/user_agent identify the real caller behind
            # the ngrok proxy (e.g. PostmanRuntime vs the cloud routine).
            forwarded_for = request.headers.get("X-Forwarded-For")
            user_agent = request.headers.get("User-Agent")
            _audit_auth_failure(inbox, {
                "ts": datetime.now(timezone.utc).isoformat(),
                "event": "webhook_auth_failure",
                "reason": reason,
                "remote_addr": request.remote_addr,
                "forwarded_for": forwarded_for,
                "user_agent": user_agent,
                "token_present": bool(token),
                "signature_present": bool(signature),
                "body_bytes": len(raw),
                "path": request.path,
            })
            logger.warning("webhook auth failed (%s) from %s (xff=%s, ua=%s)",
                           reason, request.remote_addr, forwarded_for, user_agent)
            return jsonify({"error": "unauthorized"}), 401
        try:
            payload = RoutineSignalPayload.model_validate_json(raw)  # bad JSON or schema -> 400
        except ValidationError as e:
            # The strict shape failed. Before rejecting, try to coerce the KNOWN drifted
            # shapes (run_id/sweep_date/signals aliases) back onto the canonical schema.
            # This is NOT a relaxation of the enqueue invariant: a coerced body is
            # re-validated below, and only the validated CANONICAL bytes are enqueued —
            # so the inbox still receives a strict RoutineSignalPayload (CLAUDE.md).
            coerced = coerce_payload(raw)
            if coerced is not None:
                try:
                    payload = RoutineSignalPayload.model_validate_json(coerced)
                except ValidationError:
                    payload = None
                if payload is not None:
                    path = _atomic_enqueue(inbox, coerced)
                    logger.info("webhook enqueued (coerced) %s (routine_id=%s, %d change(s))",
                                path.name, payload.routine_id, len(payload.signal_changes))
                    return jsonify({"status": "accepted", "coerced": True,
                                    "routine_id": payload.routine_id,
                                    "signals": len(payload.signal_changes)}), 202
            # Unrecoverable. Body is PRESERVED for diagnosis + replay, and the caller
            # (already authenticated) gets field-level reasons back.
            qpath = _quarantine(inbox, raw)
            logger.warning("webhook payload rejected, quarantined %s: %s", qpath.name, e)
            detail = [{"loc": ".".join(str(p) for p in err["loc"]), "msg": err["msg"]}
                      for err in e.errors()]
            return jsonify({"error": "invalid payload", "detail": detail}), 400
        path = _atomic_enqueue(inbox, raw)
        logger.info("webhook enqueued %s (routine_id=%s, %d change(s))",
                    path.name, payload.routine_id, len(payload.signal_changes))
        return jsonify({"status": "accepted", "routine_id": payload.routine_id,
                        "signals": len(payload.signal_changes)}), 202

    return app


def run() -> int:  # pragma: no cover — live entrypoint
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    inbox_dir = os.getenv("AUTOTRADER_SIGNAL_INBOX")
    secret = os.getenv("AUTOTRADER_WEBHOOK_SECRET", "")
    if not inbox_dir:
        logger.error("AUTOTRADER_SIGNAL_INBOX must be set (shared with the trader)")
        return 2
    if not secret:
        logger.error("AUTOTRADER_WEBHOOK_SECRET must be set (config/secure.config)")
        return 2
    host = os.getenv("AUTOTRADER_WEBHOOK_HOST", "127.0.0.1")
    port = int(os.getenv("AUTOTRADER_WEBHOOK_PORT", "8799"))
    max_body = int(os.getenv("AUTOTRADER_WEBHOOK_MAX_BODY", str(_MAX_BODY_DEFAULT)))
    app = create_app(inbox_dir, secret, max_body)
    logger.info("webhook on %s:%d -> inbox %s (localhost-only; expose via `ngrok http %d`)",
                host, port, inbox_dir, port)
    app.run(host=host, port=port, debug=False, use_reloader=False)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(run())
