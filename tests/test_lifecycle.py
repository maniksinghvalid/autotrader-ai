"""Lifecycle job actions: ground_truth_sync writes broker truth into the 2a
SQLite projection; EntryGate tracks whether new entries are permitted."""
from autotrader.db import DB
from autotrader.sim_broker import SimBroker
from autotrader.domain import OrderRequest


def test_ground_truth_sync_upserts_positions_and_records_fills(tmp_path):
    from autotrader.lifecycle import ground_truth_sync
    db = DB(str(tmp_path / "gt.db"))
    b = SimBroker(quotes={"US.AAPL": 150.0}, cash=100000.0)
    # one filled buy -> SimBroker now holds a position and has a fill
    b.place_order(OrderRequest(symbol="US.AAPL", side="BUY", qty=10,
                               order_type="LIMIT", limit_price=150.0,
                               client_order_id="seed"))
    res = ground_truth_sync(b, db, since=None)
    assert res.positions == 1
    assert res.new_fills == 1
    pos = db._conn.execute("SELECT qty FROM positions WHERE symbol='US.AAPL'").fetchone()
    assert pos[0] == 10
    db.close()


def test_ground_truth_sync_is_idempotent_on_fills(tmp_path):
    from autotrader.lifecycle import ground_truth_sync
    db = DB(str(tmp_path / "gt.db"))
    b = SimBroker(quotes={"US.AAPL": 150.0}, cash=100000.0)
    b.place_order(OrderRequest(symbol="US.AAPL", side="BUY", qty=5,
                               order_type="LIMIT", limit_price=150.0,
                               client_order_id="seed"))
    assert ground_truth_sync(b, db, since=None).new_fills == 1
    assert ground_truth_sync(b, db, since=None).new_fills == 0, "duplicate fills dedupe"
    db.close()


def test_ground_truth_sync_removes_vanished_positions(tmp_path):
    from autotrader.lifecycle import ground_truth_sync
    db = DB(str(tmp_path / "gt.db"))
    # A stale row left over from a prior session — the broker no longer holds it.
    db._conn.execute(
        "INSERT INTO positions (symbol,qty,avg_price,updated_at) VALUES (?,?,?,?)",
        ("US.GONE", 7, 20.0, "2026-06-15T20:30:00+00:00"))
    db._conn.commit()
    b = SimBroker(quotes={"US.AAPL": 150.0}, cash=100000.0)
    b.place_order(OrderRequest(symbol="US.AAPL", side="BUY", qty=10,
                               order_type="LIMIT", limit_price=150.0,
                               client_order_id="seed"))
    ground_truth_sync(b, db, since=None)
    gone = db._conn.execute("SELECT 1 FROM positions WHERE symbol='US.GONE'").fetchone()
    assert gone is None, "a position absent from the broker snapshot must be reconciled away"
    aapl = db._conn.execute("SELECT qty FROM positions WHERE symbol='US.AAPL'").fetchone()
    assert aapl[0] == 10
    db.close()


def test_entry_gate_defaults_closed_and_opens():
    from autotrader.lifecycle import EntryGate
    g = EntryGate()
    assert g.entries_enabled is False
    g.open()
    assert g.entries_enabled is True


def test_entry_gate_close_disables():
    from autotrader.lifecycle import EntryGate
    g = EntryGate(enabled=True)
    g.close()
    assert g.entries_enabled is False
