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


def test_reconcile_open_orders_noop_when_book_unknown(tmp_path):
    """A failed open-orders query must NOT mark DB stops cancelled — the old
    []-on-failure behavior silently declared every working stop dead."""
    from autotrader.lifecycle import reconcile_open_orders
    db = DB(str(tmp_path / "t.db"))
    db.record_trade(client_order_id="c1", symbol="US.AAPL", side="SELL", qty=5,
                    order_type="TRAILING_STOP", limit_price=None,
                    broker_order_id="sim-9", state="SUBMITTED")
    b = SimBroker(quotes={"US.AAPL": 100.0})
    b.fail_open_orders = True
    assert reconcile_open_orders(b, db) == 0
    assert db.get_open_trailing_stop("US.AAPL") == "sim-9"   # still live in the projection
    db.close()


def test_ground_truth_sync_skips_fills_when_query_fails(tmp_path):
    from autotrader.lifecycle import ground_truth_sync
    db = DB(str(tmp_path / "t.db"))
    b = SimBroker(quotes={"US.AAPL": 100.0}, cash=10000.0)
    b.place_order(OrderRequest(symbol="US.AAPL", side="BUY", qty=1,
                               order_type="MARKET", limit_price=None,
                               client_order_id="seed"))
    b.fail_reconcile_fills = True
    res = ground_truth_sync(b, db)
    assert res.new_fills == 0
    assert db._conn.execute("SELECT COUNT(*) FROM fills").fetchone()[0] == 0
    db.close()


def test_ground_truth_sync_keeps_positions_when_positions_not_loaded(tmp_path):
    """A stale/not-loaded snapshot (positions query failed) must not wipe the
    positions projection with its empty tuple."""
    from autotrader.lifecycle import ground_truth_sync
    from autotrader.domain import AccountSnapshot
    db = DB(str(tmp_path / "t.db"))
    good = SimBroker(quotes={"US.AAPL": 100.0}, cash=10000.0)
    good.place_order(OrderRequest(symbol="US.AAPL", side="BUY", qty=3,
                                  order_type="MARKET", limit_price=None,
                                  client_order_id="seed"))
    ground_truth_sync(good, db)                    # projection now holds AAPL x3

    class _BrokenPositions(SimBroker):
        def get_account(self):
            return AccountSnapshot(cash=1.0, total_assets=1.0, day_pnl=0.0,
                                   stale=True, positions_loaded=False, positions=())

    broken = _BrokenPositions(quotes={"US.AAPL": 100.0})
    ground_truth_sync(broken, db)
    row = db._conn.execute("SELECT qty FROM positions WHERE symbol='US.AAPL'").fetchone()
    assert row is not None and row[0] == 3         # NOT wiped
    db.close()
