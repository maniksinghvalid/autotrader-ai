"""strategy_claims persistence + held_symbols reconcile helper."""
from autotrader.db import DB
from autotrader.domain import Position


def _db(tmp_path, name="t.db"):
    return DB(str(tmp_path / name))


def test_claim_release_roundtrip(tmp_path):
    db = _db(tmp_path)
    db.claim_symbol("US.NIO", "BREAKOUT", "sess-1")
    assert db.get_claims() == {"US.NIO": "BREAKOUT"}
    db.release_claim("US.NIO")
    assert db.get_claims() == {}
    db.close()


def test_claim_is_idempotent(tmp_path):
    db = _db(tmp_path)
    db.claim_symbol("US.NIO", "BREAKOUT", "sess-1")
    db.claim_symbol("US.NIO", "BREAKOUT", "sess-2")  # re-claim, no error
    assert db.get_claims() == {"US.NIO": "BREAKOUT"}
    db.close()


def test_release_absent_is_noop(tmp_path):
    db = _db(tmp_path)
    db.release_claim("US.NIO")  # must not raise
    assert db.get_claims() == {}
    db.close()


def test_claims_persist_across_reopen(tmp_path):
    db = _db(tmp_path)
    db.claim_symbol("US.NIO", "BREAKOUT", "sess-1")
    db.close()
    db2 = DB(str(tmp_path / "t.db"))
    assert db2.get_claims() == {"US.NIO": "BREAKOUT"}
    db2.close()


def test_held_symbols_reflects_positions_with_positive_qty(tmp_path):
    db = _db(tmp_path)
    db.replace_positions([Position("US.NIO", 10, 5.0), Position("US.AAPL", 0, 100.0)])
    assert db.held_symbols() == {"US.NIO"}
    db.close()
