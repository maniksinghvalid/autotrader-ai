from autotrader.db import DB


def _db(tmp_path):
    return DB(str(tmp_path / "t.db"))


def test_upsert_and_latest_target_weights(tmp_path):
    db = _db(tmp_path)
    assert db.latest_target_weights() is None
    db.upsert_target_weights("2026-06-15", [("US.AAPL", 10.0), ("US.MSFT", 20.0)])
    db.upsert_target_weights("2026-06-16", [("US.AAPL", 80.0), ("US.MSFT", 60.0)])
    as_of, ingested_at, scores = db.latest_target_weights()
    assert as_of == "2026-06-16"
    assert scores == {"US.AAPL": 80.0, "US.MSFT": 60.0}
    assert ingested_at  # ISO timestamp present
    db.close()


def test_upsert_same_date_replaces(tmp_path):
    db = _db(tmp_path)
    db.upsert_target_weights("2026-06-16", [("US.AAPL", 10.0)])
    db.upsert_target_weights("2026-06-16", [("US.AAPL", 99.0)])
    _, _, scores = db.latest_target_weights()
    assert scores == {"US.AAPL": 99.0}
    db.close()


def test_open_trailing_stop_lifecycle(tmp_path):
    db = _db(tmp_path)
    assert db.get_open_trailing_stop("US.AAPL") is None
    db.record_trade(client_order_id="c1", symbol="US.AAPL", side="SELL", qty=5,
                    order_type="TRAILING_STOP", limit_price=None,
                    broker_order_id="b1", state="SUBMITTED")
    assert db.get_open_trailing_stop("US.AAPL") == "b1"
    db.mark_order_cancelled("b1")
    assert db.get_open_trailing_stop("US.AAPL") is None
    db.close()


def test_open_trailing_stop_ignores_non_stop_and_filled(tmp_path):
    db = _db(tmp_path)
    db.record_trade(client_order_id="c2", symbol="US.AAPL", side="BUY", qty=5,
                    order_type="MARKET", limit_price=None,
                    broker_order_id="b2", state="FILLED")
    db.record_trade(client_order_id="c3", symbol="US.AAPL", side="SELL", qty=5,
                    order_type="TRAILING_STOP", limit_price=None,
                    broker_order_id="b3", state="FILLED")
    assert db.get_open_trailing_stop("US.AAPL") is None
    db.close()
