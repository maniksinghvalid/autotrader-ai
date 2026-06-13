"""Lifecycle job actions and the entry-window gate.

ground_truth_sync pulls broker truth (positions + fills) into the SQLite
projection from Plan 2a — the broker is the source of truth, the projection a
read-through cache (research: overwrite local drift with broker state). Fills
dedupe by fill_id inside DB.record_fills, so re-running is safe.

EntryGate gates *new* entries (BUY). Exits (SELL) are never gated — you must
always be able to flatten a position."""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

from autotrader.broker import Broker
from autotrader.db import DB

logger = logging.getLogger("autotrader.lifecycle")


@dataclass(frozen=True)
class SyncResult:
    positions: int
    new_fills: int


def ground_truth_sync(broker: Broker, db: DB, since: Optional[str] = None) -> SyncResult:
    snap = broker.get_account()
    db.upsert_positions(list(snap.positions))
    fills = broker.reconcile_fills(since)
    new = db.record_fills(fills)
    logger.info("ground_truth_sync: %d position(s), %d new fill(s)",
                len(snap.positions), new)
    return SyncResult(positions=len(snap.positions), new_fills=new)


class EntryGate:
    """Whether new BUY entries are currently permitted. Defaults closed."""

    def __init__(self, enabled: bool = False):
        self._enabled = enabled

    @property
    def entries_enabled(self) -> bool:
        return self._enabled

    def open(self) -> None:
        self._enabled = True

    def close(self) -> None:
        self._enabled = False
