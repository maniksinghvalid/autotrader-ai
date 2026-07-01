"""SQLite WAL projection — an incrementally-populated read-through cache of
broker ground truth.

Tables:
  signals     — every Signal value that passed the confidence filter
  trades      — every OrderRequest + OrderAck (keyed by client_order_id)
  fills       — every Fill received from the broker (keyed by fill_id, idempotent)
  positions   — latest position snapshot per symbol (upserted by symbol)
  performance — one row per calendar date (upserted)
  halts       — soft-halt events with optional resolution timestamp

The JSONL audit journal is the immutable source of record for orders; this
projection is rebuilt from it on startup in Phase 3+.
"""
from __future__ import annotations

import logging
import sqlite3
import threading
from datetime import date, datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, List, Optional

if TYPE_CHECKING:
    from autotrader.domain import Fill, Position

logger = logging.getLogger("autotrader.db")


_SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS signals (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    ts            TEXT NOT NULL,
    symbol        TEXT NOT NULL,
    direction     TEXT NOT NULL CHECK(direction IN ('BUY','SELL')),
    confidence    REAL NOT NULL,
    rationale     TEXT NOT NULL,
    signal_id     TEXT NOT NULL UNIQUE
);

CREATE TABLE IF NOT EXISTS trades (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    ts              TEXT NOT NULL,
    client_order_id TEXT NOT NULL UNIQUE,
    symbol          TEXT NOT NULL,
    side            TEXT NOT NULL CHECK(side IN ('BUY','SELL')),
    qty             INTEGER NOT NULL,
    order_type      TEXT NOT NULL,
    limit_price     REAL,
    broker_order_id TEXT,
    state           TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS fills (
    fill_id   TEXT PRIMARY KEY,
    ts        TEXT NOT NULL,
    symbol    TEXT NOT NULL,
    side      TEXT NOT NULL CHECK(side IN ('BUY','SELL')),
    qty       REAL NOT NULL,
    price     REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS positions (
    symbol     TEXT PRIMARY KEY,
    qty        INTEGER NOT NULL,
    avg_price  REAL NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS halts (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    ts           TEXT NOT NULL,
    reason       TEXT NOT NULL,
    resolved_at  TEXT
);

CREATE TABLE IF NOT EXISTS target_weights (
    as_of_date  TEXT NOT NULL,
    symbol      TEXT NOT NULL,
    score       REAL NOT NULL,
    ingested_at TEXT NOT NULL,
    PRIMARY KEY (as_of_date, symbol)
);

CREATE TABLE IF NOT EXISTS drivers (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    ts      TEXT NOT NULL,
    symbol  TEXT NOT NULL,
    side    TEXT NOT NULL CHECK(side IN ('BUY','SELL')),
    kind    TEXT NOT NULL,         -- non-signal trade driver, e.g. 'rebalance'
    detail  TEXT NOT NULL          -- human reason, e.g. 'rbal-2026-06-17 · overweight → trim'
);
"""

_PERFORMANCE_TABLE = """
CREATE TABLE IF NOT EXISTS performance (
    date           TEXT PRIMARY KEY,
    day_pnl        REAL,
    total_assets   REAL NOT NULL,
    cash           REAL NOT NULL,
    gross_exposure REAL,
    unrealized_pnl REAL NOT NULL DEFAULT 0,
    updated_at     TEXT NOT NULL
);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _today() -> str:
    return date.today().isoformat()


class DB:
    def __init__(self, path: str):
        db_dir = Path(path).parent
        db_dir.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.executescript(_SCHEMA + _PERFORMANCE_TABLE)
        cols = [r[1] for r in self._conn.execute("PRAGMA table_info(performance)")]
        if "unrealized_pnl" not in cols:
            self._conn.execute(
                "ALTER TABLE performance ADD COLUMN unrealized_pnl REAL NOT NULL DEFAULT 0")
        # gross_exposure must be nullable to represent "exposure unknown"
        # (positions failed to load); legacy DBs created it NOT NULL.
        # day_pnl must be nullable to represent "realized unavailable" (reporter
        # renders '-' rather than a fabricated 0.00); legacy/Task-2-migrated DBs
        # may still have it NOT NULL, so check both columns independently — a DB
        # already rebuilt by the gross_exposure migration won't re-trigger on
        # that clause alone once day_pnl is the only NOT NULL holdout.
        perf_sql = self._conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='performance'"
        ).fetchone()
        if perf_sql and (
            "gross_exposure REAL NOT NULL" in perf_sql[0]
            or "day_pnl REAL NOT NULL" in perf_sql[0]
        ):
            self._conn.executescript(
                "ALTER TABLE performance RENAME TO performance_old;"
                + _PERFORMANCE_TABLE +
                "INSERT INTO performance "
                "(date,day_pnl,total_assets,cash,gross_exposure,unrealized_pnl,updated_at) "
                "SELECT date,day_pnl,total_assets,cash,gross_exposure,unrealized_pnl,updated_at "
                "FROM performance_old;"
                "DROP TABLE performance_old;")
        self._conn.commit()
        self._lock = threading.Lock()

    def record_signal(self, symbol: str, direction: str, confidence: float,
                      rationale: str, signal_id: str) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT OR IGNORE INTO signals "
                "(ts,symbol,direction,confidence,rationale,signal_id) VALUES (?,?,?,?,?,?)",
                (_now(), symbol, direction, confidence, rationale, signal_id),
            )
            self._conn.commit()

    def record_driver(self, symbol: str, side: str, kind: str, detail: str) -> None:
        """Record a non-signal trade driver (e.g. a rebalance trim/top-up) so the
        EOD report can attribute a trade that never produced a domain.Signal."""
        with self._lock:
            self._conn.execute(
                "INSERT INTO drivers (ts,symbol,side,kind,detail) VALUES (?,?,?,?,?)",
                (_now(), symbol, side, kind, detail),
            )
            self._conn.commit()

    def record_trade(self, client_order_id: str, symbol: str, side: str, qty: int,
                     order_type: str, limit_price: Optional[float],
                     broker_order_id: Optional[str], state: str) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO trades "
                "(ts,client_order_id,symbol,side,qty,order_type,limit_price,"
                "broker_order_id,state) VALUES (?,?,?,?,?,?,?,?,?)",
                (_now(), client_order_id, symbol, side, qty, order_type,
                 limit_price, broker_order_id, state),
            )
            self._conn.commit()

    def record_fills(self, fills: List) -> int:
        """Upsert fills by fill_id (idempotent). Returns count of newly inserted rows."""
        inserted = 0
        with self._lock:
            for f in fills:
                cur = self._conn.execute(
                    "INSERT OR IGNORE INTO fills (fill_id,ts,symbol,side,qty,price) "
                    "VALUES (?,?,?,?,?,?)",
                    (f.fill_id, f.ts, f.symbol, f.side, f.qty, f.price),
                )
                inserted += cur.rowcount
            self._conn.commit()
        return inserted

    def upsert_positions(self, positions: List) -> None:
        ts = _now()
        with self._lock:
            for p in positions:
                self._conn.execute(
                    "INSERT OR REPLACE INTO positions (symbol,qty,avg_price,updated_at) "
                    "VALUES (?,?,?,?)",
                    (p.symbol, p.qty, p.avg_price, ts),
                )
            self._conn.commit()

    def replace_positions(self, positions: List) -> None:
        """Reconcile the positions table to mirror the broker snapshot: upsert
        every position present, then delete any symbol no longer in the snapshot.
        The broker omits fully-flat positions rather than reporting qty=0, so a
        closed-out symbol must be removed here or it lingers forever (read-through
        cache posture: broker is the source of truth). An empty snapshot clears
        the table."""
        ts = _now()
        keep = [p.symbol for p in positions]
        with self._lock:
            for p in positions:
                self._conn.execute(
                    "INSERT OR REPLACE INTO positions (symbol,qty,avg_price,updated_at) "
                    "VALUES (?,?,?,?)",
                    (p.symbol, p.qty, p.avg_price, ts),
                )
            if keep:
                placeholders = ",".join("?" for _ in keep)
                self._conn.execute(
                    f"DELETE FROM positions WHERE symbol NOT IN ({placeholders})",
                    keep,
                )
            else:
                self._conn.execute("DELETE FROM positions")
            self._conn.commit()

    def record_performance(self, day_pnl: float, total_assets: float,
                           cash: float, gross_exposure: Optional[float],
                           unrealized_pnl: float = 0.0, *,
                           positions_loaded: bool = True) -> None:
        # Invariant: never persist a fabricated gross_exposure when positions did
        # not load. A failed snapshot stores NULL ("exposure unknown"), never 0.
        if not positions_loaded:
            if gross_exposure not in (None, 0, 0.0):
                logger.warning(
                    "record_performance: coercing gross_exposure=%s to NULL "
                    "(positions_loaded is False)", gross_exposure)
            gross_exposure = None
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO performance "
                "(date,day_pnl,total_assets,cash,gross_exposure,unrealized_pnl,updated_at) "
                "VALUES (?,?,?,?,?,?,?)",
                (_today(), day_pnl, total_assets, cash, gross_exposure,
                 unrealized_pnl, _now()),
            )
            self._conn.commit()

    def record_halt(self, reason: str) -> int:
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO halts (ts,reason) VALUES (?,?)", (_now(), reason)
            )
            self._conn.commit()
        return cur.lastrowid

    def resolve_halt(self, halt_id: int) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE halts SET resolved_at=? WHERE id=?", (_now(), halt_id)
            )
            self._conn.commit()

    def upsert_target_weights(self, as_of_date: str,
                              rows: List[tuple]) -> None:
        """Replace the target-weight snapshot for as_of_date. rows = [(symbol, score)].
        ingested_at is stamped now (UTC) and drives the rebalance staleness guard."""
        ts = _now()
        with self._lock:
            self._conn.execute("DELETE FROM target_weights WHERE as_of_date=?",
                               (as_of_date,))
            for symbol, score in rows:
                self._conn.execute(
                    "INSERT INTO target_weights (as_of_date,symbol,score,ingested_at) "
                    "VALUES (?,?,?,?)",
                    (as_of_date, symbol.upper(), float(score), ts),
                )
            self._conn.commit()

    def latest_target_weights(self):
        """Return (as_of_date, ingested_at, {symbol: score}) for the newest
        snapshot, or None if none stored."""
        with self._lock:
            row = self._conn.execute(
                "SELECT as_of_date FROM target_weights "
                "ORDER BY as_of_date DESC LIMIT 1").fetchone()
            if row is None:
                return None
            as_of = row[0]
            rows = self._conn.execute(
                "SELECT symbol, score, ingested_at FROM target_weights "
                "WHERE as_of_date=?", (as_of,)).fetchall()
        scores = {r[0]: r[1] for r in rows}
        ingested_at = rows[0][2]
        return as_of, ingested_at, scores

    def get_open_trailing_stop(self, symbol: str):
        """broker_order_id of the most recent working TRAILING_STOP SELL for
        symbol, or None. Working = SUBMITTED/PARTIAL with a broker id."""
        with self._lock:
            row = self._conn.execute(
                "SELECT broker_order_id FROM trades "
                "WHERE symbol=? AND order_type='TRAILING_STOP' AND side='SELL' "
                "AND state IN ('SUBMITTED','PARTIAL') AND broker_order_id IS NOT NULL "
                "ORDER BY id DESC LIMIT 1", (symbol,)).fetchone()
        return row[0] if row else None

    def mark_order_cancelled(self, broker_order_id: str) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE trades SET state='CANCELLED' WHERE broker_order_id=?",
                (broker_order_id,))
            self._conn.commit()

    def open_trailing_stop_ids(self) -> List[str]:
        """broker_order_ids of ALL working TRAILING_STOP SELLs (state
        SUBMITTED/PARTIAL). Used by reconcile to detect stops swept at the broker."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT broker_order_id FROM trades "
                "WHERE order_type='TRAILING_STOP' AND side='SELL' "
                "AND state IN ('SUBMITTED','PARTIAL') AND broker_order_id IS NOT NULL"
            ).fetchall()
        return [r[0] for r in rows]

    def close(self) -> None:
        self._conn.close()
