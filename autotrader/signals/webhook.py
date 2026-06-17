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
import logging
import os
import tempfile
import uuid
from pathlib import Path
from typing import Optional

from flask import Flask, jsonify, request
from pydantic import ValidationError

from autotrader.signals.coerce import coerce_payload
from autotrader.signals.schema import RoutineSignalPayload

logger = logging.getLogger("autotrader.signals.webhook")

_MAX_BODY_DEFAULT = 65536


def _verify(secret: str, raw: bytes, token: Optional[str], signature: Optional[str]) -> bool:
    """Constant-time check: token must equal the secret AND signature must equal
    'sha256=' + HMAC-SHA256(secret, raw_body). Empty secret => always reject."""
    if not secret or not token or not signature:
        return False
    if not hmac.compare_digest(token, secret):
        return False
    expected = "sha256=" + hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()
    return hmac.compare_digest(signature, expected)


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
        if not _verify(secret, raw,
                       request.headers.get("X-Webhook-Token"),
                       request.headers.get("X-Webhook-Signature")):
            logger.warning("webhook auth failed from %s", request.remote_addr)
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
