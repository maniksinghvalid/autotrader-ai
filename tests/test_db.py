"""SQLite WAL projection tests. All use tmp_path to avoid stale state."""
import pytest


def _db(tmp_path, name="test.db"):
    from autotrader.db import DB
    return DB(str(tmp_path / name))


def test_record_signal_stores_and_dedupes_by_signal_id(tmp_path):
    db = _db(tmp_path)
    db.record_signal("US.AAPL", "BUY", 0.8, "above entry", "sig-1")
    db.record_signal("US.AAPL", "BUY", 0.9, "still above", "sig-1")  # duplicate
    count = db._conn.execute("SELECT COUNT(*) FROM signals").fetchone()[0]
    assert count == 1, "INSERT OR IGNORE must dedupe on signal_id"
    db.close()


def test_record_trade_stores_and_replaces_on_duplicate_cid(tmp_path):
    db = _db(tmp_path)
    db.record_trade("cid-1", "US.AAPL", "BUY", 10, "MARKET", None, "boid-1", "SUBMITTED")
    db.record_trade("cid-1", "US.AAPL", "BUY", 10, "MARKET", None, "boid-1", "FILLED")
    row = db._conn.execute("SELECT state FROM trades WHERE client_order_id='cid-1'").fetchone()
    assert row[0] == "FILLED", "INSERT OR REPLACE must update state on duplicate cid"
    db.close()


def test_record_fills_idempotent_on_duplicate_fill_id(tmp_path):
    from autotrader.domain import Fill
    db = _db(tmp_path)
    fills = [Fill("fid-1", "US.AAPL", "BUY", 5.0, 150.0, "2026-06-12T10:00:00+00:00")]
    inserted = db.record_fills(fills)
    assert inserted == 1
    inserted_again = db.record_fills(fills)  # duplicate
    assert inserted_again == 0, "INSERT OR IGNORE must dedupe on fill_id"
    db.close()


def test_upsert_positions_stores_and_overwrites(tmp_path):
    from autotrader.domain import Position
    db = _db(tmp_path)
    db.upsert_positions([Position("US.AAPL", 10, 150.0)])
    db.upsert_positions([Position("US.AAPL", 15, 152.0)])  # update
    row = db._conn.execute("SELECT qty, avg_price FROM positions WHERE symbol='US.AAPL'").fetchone()
    assert row == (15, 152.0)
    db.close()


def test_replace_positions_reconciles_to_snapshot(tmp_path):
    from autotrader.domain import Position
    db = _db(tmp_path)
    db.replace_positions([Position("US.AAPL", 10, 150.0), Position("US.MSFT", 5, 400.0)])
    # MSFT is fully closed -> absent from the new snapshot; AAPL updated.
    db.replace_positions([Position("US.AAPL", 12, 151.0)])
    rows = db._conn.execute("SELECT symbol, qty FROM positions ORDER BY symbol").fetchall()
    assert rows == [("US.AAPL", 12)], "symbols absent from the snapshot must be removed"
    db.close()


def test_replace_positions_empty_snapshot_clears_table(tmp_path):
    from autotrader.domain import Position
    db = _db(tmp_path)
    db.replace_positions([Position("US.AAPL", 10, 150.0)])
    db.replace_positions([])  # broker now flat
    count = db._conn.execute("SELECT COUNT(*) FROM positions").fetchone()[0]
    assert count == 0, "an empty snapshot clears the positions table"
    db.close()


def test_record_performance_upserts_by_date(tmp_path):
    db = _db(tmp_path)
    db.record_performance(day_pnl=100.0, total_assets=10500.0, cash=500.0, gross_exposure=10000.0)
    db.record_performance(day_pnl=200.0, total_assets=10600.0, cash=400.0, gross_exposure=10200.0)
    count = db._conn.execute("SELECT COUNT(*) FROM performance").fetchone()[0]
    assert count == 1, "performance upserts by date (one row per trading day)"
    row = db._conn.execute("SELECT day_pnl FROM performance").fetchone()
    assert row[0] == 200.0, "second upsert should overwrite day_pnl"
    db.close()


def test_record_performance_writes_null_gross_when_positions_not_loaded(tmp_path):
    db = _db(tmp_path)
    db.record_performance(day_pnl=0.0, total_assets=1000.0, cash=1000.0,
                          gross_exposure=0.0, positions_loaded=False)
    row = db._conn.execute("SELECT gross_exposure FROM performance").fetchone()
    assert row[0] is None, "false 0% must not be persisted when positions failed to load"
    db.close()


def test_record_performance_accepts_explicit_none_gross(tmp_path):
    db = _db(tmp_path)
    db.record_performance(day_pnl=0.0, total_assets=1000.0, cash=1000.0,
                          gross_exposure=None, positions_loaded=False)
    row = db._conn.execute("SELECT gross_exposure FROM performance").fetchone()
    assert row[0] is None
    db.close()


def test_record_performance_stores_real_gross_when_loaded(tmp_path):
    db = _db(tmp_path)
    db.record_performance(day_pnl=5.0, total_assets=1000.0, cash=900.0,
                          gross_exposure=100.0, positions_loaded=True)
    row = db._conn.execute("SELECT gross_exposure FROM performance").fetchone()
    assert row[0] == 100.0
    db.close()


def test_record_halt_and_resolve(tmp_path):
    db = _db(tmp_path)
    halt_id = db.record_halt("daily loss limit breached")
    resolved = db._conn.execute(
        "SELECT resolved_at FROM halts WHERE id=?", (halt_id,)
    ).fetchone()[0]
    assert resolved is None, "halt should be unresolved on creation"
    db.resolve_halt(halt_id)
    resolved = db._conn.execute(
        "SELECT resolved_at FROM halts WHERE id=?", (halt_id,)
    ).fetchone()[0]
    assert resolved is not None, "resolve_halt must set resolved_at"
    db.close()


def test_db_creates_parent_directory_if_missing(tmp_path):
    from autotrader.db import DB
    nested = str(tmp_path / "nested" / "deep" / "autotrader.db")
    db = DB(nested)
    db.record_halt("test")
    count = db._conn.execute("SELECT COUNT(*) FROM halts").fetchone()[0]
    assert count == 1
    db.close()
