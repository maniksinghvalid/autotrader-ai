"""Stop reconciliation: the broker is the source of truth for resting orders.
A cancel_all at EOD or on a hard-loss halt sweeps trailing stops at the broker;
reconcile_open_orders (and ground_truth_sync, which calls it) brings the SQLite
projection in line so get_open_trailing_stop never treats a dead stop as live."""
from autotrader.db import DB
from autotrader.domain import OrderRequest
from autotrader.lifecycle import ground_truth_sync, reconcile_open_orders
from autotrader.sim_broker import SimBroker


def _stop_in_db(db, broker, symbol="US.AAPL", qty=10):
    """Place a resting trailing stop at the broker and mirror it in the DB."""
    ack = broker.place_order(OrderRequest(symbol, "SELL", qty, "TRAILING_STOP",
                                          None, f"c-{symbol}", trail_percent=5.0))
    db.record_trade(client_order_id=f"c-{symbol}", symbol=symbol, side="SELL",
                    qty=qty, order_type="TRAILING_STOP", limit_price=None,
                    broker_order_id=ack.broker_order_id, state=ack.state.value)
    return ack.broker_order_id


def test_open_trailing_stop_ids_lists_working_stops(tmp_path):
    db = DB(str(tmp_path / "t.db"))
    broker = SimBroker({"US.AAPL": 100.0})
    boid = _stop_in_db(db, broker)
    assert db.open_trailing_stop_ids() == [boid]
    db.close()


def test_reconcile_leaves_live_stop(tmp_path):
    db = DB(str(tmp_path / "t.db"))
    broker = SimBroker({"US.AAPL": 100.0})
    boid = _stop_in_db(db, broker)
    assert reconcile_open_orders(broker, db) == 0   # still open at broker
    assert db.get_open_trailing_stop("US.AAPL") == boid
    db.close()


def test_reconcile_marks_swept_stop_cancelled(tmp_path):
    db = DB(str(tmp_path / "t.db"))
    broker = SimBroker({"US.AAPL": 100.0})
    _stop_in_db(db, broker)
    broker.cancel_all()                              # EOD / halt sweep
    assert reconcile_open_orders(broker, db) == 1
    assert db.get_open_trailing_stop("US.AAPL") is None
    db.close()


def test_ground_truth_sync_reconciles_swept_stops(tmp_path):
    # After a cancel_all, the next ground-truth sync (startup PRE_OPEN_SYNC,
    # RISK_SWEEP, or watchdog recovery) reconciles the projection.
    db = DB(str(tmp_path / "t.db"))
    broker = SimBroker({"US.AAPL": 100.0})
    _stop_in_db(db, broker)
    broker.cancel_all()
    ground_truth_sync(broker, db)
    assert db.get_open_trailing_stop("US.AAPL") is None
    db.close()
