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
    if snap.positions_loaded:
        db.replace_positions(list(snap.positions))
    else:
        # Position query failed — snap.positions is an EMPTY placeholder, not
        # broker truth. Replacing would wipe the projection with nothing.
        logger.warning("ground_truth_sync: positions not loaded — projection kept as-is")
    fills = broker.reconcile_fills(since)
    if fills is None:
        logger.warning("ground_truth_sync: fills query failed — skipping fill projection")
        new = 0
    else:
        new = db.record_fills(fills)
    reconcile_open_orders(broker, db)
    logger.info("ground_truth_sync: %d position(s), %d new fill(s)",
                len(snap.positions), new)
    return SyncResult(positions=len(snap.positions), new_fills=new)


def reconcile_open_orders(broker: Broker, db: DB) -> int:
    """Bring the trades projection in line with broker truth for resting trailing
    stops: any DB-tracked working stop that is no longer open at the broker (e.g.
    swept by a cancel_all at EOD or on a hard-loss halt) is marked CANCELLED, so a
    later get_open_trailing_stop never treats a dead stop as live. The broker is
    the source of truth (read-through cache posture, db.py). Returns the count
    swept. (A stop that triggered+filled is likewise marked CANCELLED here — the
    fill itself is captured by reconcile_fills; the projection only tracks whether
    the order is still working, which is what get_open_trailing_stop needs.)

    A None from get_open_orders means the book is UNKNOWN — sweeping then would
    mark live stops CANCELLED off a failed query, so the reconcile is a logged
    no-op instead."""
    open_orders = broker.get_open_orders()
    if open_orders is None:
        logger.warning("reconcile_open_orders: open-orders query failed — no-op")
        return 0
    open_ids = {a.broker_order_id for a in open_orders
                if a.broker_order_id is not None}
    swept = 0
    for boid in db.open_trailing_stop_ids():
        if boid not in open_ids:
            db.mark_order_cancelled(boid)
            swept += 1
    if swept:
        logger.info("reconcile_open_orders: %d stale trailing stop(s) swept", swept)
    return swept


class EntryGate:
    """Whether new BUY entries are currently permitted (defaults closed), plus a
    one-way session HALT. Once halted, entries can never re-open this session and
    the runner/engine stop trading."""

    def __init__(self, enabled: bool = False):
        self._enabled = enabled
        self._halted = False

    @property
    def entries_enabled(self) -> bool:
        return self._enabled and not self._halted

    @property
    def halted(self) -> bool:
        return self._halted

    def open(self) -> None:
        self._enabled = True

    def close(self) -> None:
        self._enabled = False

    def halt(self) -> None:
        self._halted = True
        self._enabled = False


def restore_session_halt(gate: EntryGate, db: DB, today) -> bool:
    """V4c: a hard HALT survives a crash/restart. apply_risk_check persists
    halt:<date>; restoring here means a restart on a halted day starts halted
    (the date-scoped key auto-expires — the next session starts clean)."""
    raw = db.get_state(f"halt:{today.isoformat()}")
    if raw:
        gate.halt()
        logger.error("session halt RESTORED from state (%s) — trading stays "
                     "halted for the day", raw)
        return True
    return False
