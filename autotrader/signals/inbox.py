"""File-drop external-signal ingress — localhost-only by construction (a watched
directory has no network surface at all, the strongest reading of CLAUDE.md's
"no internet-reachable order path"). Drop a *.json RoutineSignalPayload into
inbox_dir; poll() validates + normalizes each into domain.Signal, then moves the
file to processed/ (ok) or rejected/ (bad). A malformed file is logged and
quarantined, never crashing the loop. Imports pydantic + domain only — no SDK."""
from __future__ import annotations

import logging
from pathlib import Path
from typing import List

from pydantic import ValidationError

from autotrader.domain import Signal
from autotrader.signals.normalize import normalize_payload
from autotrader.signals.schema import RoutineSignalPayload

logger = logging.getLogger("autotrader.signals.inbox")


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
