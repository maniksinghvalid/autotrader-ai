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

import sqlite3
import threading
from datetime import date, datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, List, Optional

if TYPE_CHECKING:
    from autotrader.domain import Fill, Position


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

CREATE TABLE IF NOT EXISTS performance (
    date           TEXT PRIMARY KEY,
    day_pnl        REAL NOT NULL,
    total_assets   REAL NOT NULL,
    cash           REAL NOT NULL,
    gross_exposure REAL NOT NULL,
    updated_at     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS halts (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    ts           TEXT NOT NULL,
    reason       TEXT NOT NULL,
    resolved_at  TEXT
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
        self._conn.executescript(_SCHEMA)
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

    def record_performance(self, day_pnl: float, total_assets: float,
                           cash: float, gross_exposure: float) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO performance "
                "(date,day_pnl,total_assets,cash,gross_exposure,updated_at) "
                "VALUES (?,?,?,?,?,?)",
                (_today(), day_pnl, total_assets, cash, gross_exposure, _now()),
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

    def close(self) -> None:
        self._conn.close()
