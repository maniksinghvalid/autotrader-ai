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
import time
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
_AUDIT_MAX_BYTES_DEFAULT = 10_000_000

# Bound on attacker-controlled audit fields (User-Agent/X-Forwarded-For) so an
# anonymous caller can't grow the audit file by sending huge header values (V6a).
_AUDIT_TRUNC = 256
# In-memory, best-effort throttle on 401 audit writes: at most _AUDIT_MAX_PER_ADDR
# records per remote_addr per _AUDIT_WINDOW_S seconds. Intentionally simple (no
# persistence, no cross-process sharing) — this bounds disk-fill from a single
# anonymous caller hammering the public URL, not a production rate-limiter (V6a).
_AUDIT_WINDOW_S = 60.0
_AUDIT_MAX_PER_ADDR = 10


class _AuditThrottle:
    def __init__(self):
        self._hits: dict[str, list[float]] = {}

    def allow(self, addr: str, now: float) -> bool:
        hits = [t for t in self._hits.get(addr, []) if now - t < _AUDIT_WINDOW_S]
        if len(hits) >= _AUDIT_MAX_PER_ADDR:
            self._hits[addr] = hits
            return False
        hits.append(now)
        self._hits[addr] = hits
        return True


def _auth_failure_reason(secret: str, token_secret: str, raw: bytes,
                         token: Optional[str], signature: Optional[str]) -> Optional[str]:
    """Return None when authenticated, else a short category naming which check
    failed (for the audit log). Same constant-time comparisons as _verify; the
    category does not leak more than the eventual 401 already does."""
    if not secret or not token_secret:
        return "server_secret_unset"
    if not token:
        return "missing_token"
    if not signature:
        return "missing_signature"
    # Bytes compare: Flask/WSGI decodes headers latin-1, and str.compare_digest
    # raises TypeError on non-ASCII str input. A non-ASCII header must resolve to
    # an audited 401, never an unaudited TypeError -> 500 (V6a).
    if not hmac.compare_digest(token.encode("utf-8", "replace"),
                               token_secret.encode("utf-8")):
        return "token_mismatch"
    expected = "sha256=" + hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(signature.encode("utf-8", "replace"),
                               expected.encode("utf-8")):
        return "bad_signature"
    return None


def _verify(secret: str, token_secret: str, raw: bytes, token: Optional[str],
            signature: Optional[str]) -> bool:
    """Constant-time check: token must equal the bearer secret AND signature must
    equal 'sha256=' + HMAC-SHA256(signing secret, raw_body). Empty secret =>
    always reject."""
    return _auth_failure_reason(secret, token_secret, raw, token, signature) is None


def _audit_auth_failure(inbox_dir: Path, record: dict,
                        audit_max_bytes: int = _AUDIT_MAX_BYTES_DEFAULT) -> None:
    """Append one JSONL line (reason + caller metadata) for a rejected request to
    <inbox>/audit/webhook-auth.jsonl. Best-effort: an audit failure must NEVER
    break the security response, so errors are swallowed (logged only). Records
    who/why — never the secret or the provided token/signature values. Skips the
    write once the audit file exceeds audit_max_bytes, bounding disk usage from
    an anonymous caller hammering the endpoint (V6a)."""
    try:
        audit_dir = inbox_dir / _AUTH_AUDIT_SUBDIR
        audit_dir.mkdir(parents=True, exist_ok=True)
        audit_file = audit_dir / _AUTH_AUDIT_FILE
        if audit_file.exists() and audit_file.stat().st_size > audit_max_bytes:
            logger.warning("webhook auth audit file exceeds %d bytes; dropping record",
                           audit_max_bytes)
            return
        line = json.dumps(record, separators=(",", ":"), ensure_ascii=False)
        with open(audit_file, "a", encoding="utf-8") as f:
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


def create_app(inbox_dir: str, secret: str, max_body: int = _MAX_BODY_DEFAULT,
              token: Optional[str] = None,
              audit_max_bytes: int = _AUDIT_MAX_BYTES_DEFAULT) -> Flask:
    app = Flask("autotrader-webhook")
    app.config["MAX_CONTENT_LENGTH"] = max_body  # Flask returns 413 past this
    inbox = Path(os.path.expanduser(inbox_dir))
    token_secret = token or secret
    if not token:
        logger.warning("single-secret mode: token == signing key — "
                       "set AUTOTRADER_WEBHOOK_TOKEN")
    audit_throttle = _AuditThrottle()

    @app.get("/healthz")
    def healthz():
        return jsonify({"status": "ok"}), 200

    @app.post("/webhook/sweep")
    def sweep():
        raw = request.get_data(cache=False)  # raw bytes — HMAC must match exactly
        token = request.headers.get("X-Webhook-Token")
        signature = request.headers.get("X-Webhook-Signature")
        reason = _auth_failure_reason(secret, token_secret, raw, token, signature)
        if reason is not None:
            # Persist who/why so a 401 is diagnosable from disk (terminal logs are
            # ephemeral). forwarded_for/user_agent identify the real caller behind
            # the ngrok proxy (e.g. PostmanRuntime vs the cloud routine). Both are
            # attacker-controlled, so truncate before persisting (V6a).
            forwarded_for = (request.headers.get("X-Forwarded-For") or "")[:_AUDIT_TRUNC] or None
            user_agent = (request.headers.get("User-Agent") or "")[:_AUDIT_TRUNC] or None
            remote_addr = request.remote_addr or "?"
            if audit_throttle.allow(remote_addr, time.monotonic()):
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
                }, audit_max_bytes=audit_max_bytes)
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
    token = os.getenv("AUTOTRADER_WEBHOOK_TOKEN") or None
    if not inbox_dir:
        logger.error("AUTOTRADER_SIGNAL_INBOX must be set (shared with the trader)")
        return 2
    if not secret:
        logger.error("AUTOTRADER_WEBHOOK_SECRET must be set (config/secure.config)")
        return 2
    host = os.getenv("AUTOTRADER_WEBHOOK_HOST", "127.0.0.1")
    port = int(os.getenv("AUTOTRADER_WEBHOOK_PORT", "8799"))
    max_body = int(os.getenv("AUTOTRADER_WEBHOOK_MAX_BODY", str(_MAX_BODY_DEFAULT)))
    app = create_app(inbox_dir, secret, max_body, token=token)
    logger.info("webhook on %s:%d -> inbox %s (localhost-only; expose via `ngrok http %d`)",
                host, port, inbox_dir, port)
    app.run(host=host, port=port, debug=False, use_reloader=False)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(run())
