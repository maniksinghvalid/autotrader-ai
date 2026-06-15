"""File-drop external-signal ingress — localhost-only by construction (a watched
directory has no network surface at all, the strongest reading of CLAUDE.md's
"no internet-reachable order path"). Drop a *.json RoutineSignalPayload into
inbox_dir; poll() validates + normalizes each into domain.Signal, then moves the
file to processed/ (ok) or rejected/ (bad). A malformed file is logged and
quarantined, never crashing the loop. Imports pydantic + domain only — no SDK."""
from __future__ import annotations

import logging
import os
import tempfile
import uuid
from pathlib import Path
from typing import List

from pydantic import ValidationError

from autotrader.domain import Signal
from autotrader.signals.normalize import normalize_payload
from autotrader.signals.schema import RoutineSignalPayload

logger = logging.getLogger("autotrader.signals.inbox")


def atomic_write_bytes(inbox_dir: Path, raw: bytes) -> Path:
    """Write raw bytes to a unique top-level *.json via temp(.part)->os.replace,
    so a concurrent poll() (which globs *.json) never observes a partial file.
    Shared by the file-drop adapter; the webhook keeps its own copy so it stays
    a self-contained, SDK-free security surface."""
    inbox_dir = Path(inbox_dir)
    inbox_dir.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(inbox_dir), prefix=".drop-", suffix=".json.part")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(raw)
            f.flush()
            os.fsync(f.fileno())
    except Exception:
        os.unlink(tmp)
        raise
    final = inbox_dir / f"drop-{uuid.uuid4().hex}.json"
    os.replace(tmp, final)  # atomic on POSIX
    return final


class SignalInbox:
    def __init__(self, inbox_dir: str, confidence_scale: float = 10.0):
        self._dir = Path(inbox_dir)
        self._processed = self._dir / "processed"
        self._rejected = self._dir / "rejected"
        for d in (self._dir, self._processed, self._rejected):
            d.mkdir(parents=True, exist_ok=True)
        self._scale = confidence_scale

    def poll(self) -> List[Signal]:
        """Validate + normalize every top-level *.json file, moving each out of
        the inbox. Returns the flattened list of normalized signals."""
        signals: List[Signal] = []
        # sorted() makes processing order deterministic (filename-ordered);
        # glob("*.json") is non-recursive, so processed/ and rejected/ are skipped.
        for path in sorted(self._dir.glob("*.json")):
            try:
                payload = RoutineSignalPayload.model_validate_json(
                    path.read_text(encoding="utf-8"))
                signals.extend(normalize_payload(payload, self._scale))
            except (ValidationError, ValueError, OSError) as e:
                logger.warning("rejected signal file %s: %s", path.name, e)
                path.replace(self._rejected / path.name)
                continue
            path.replace(self._processed / path.name)
        return signals
