from autotrader.db import DB
from autotrader.sim_broker import SimBroker


def test_account_snapshot_has_unrealized_default_zero():
    snap = SimBroker(quotes={"US.AAPL": 200.0}).get_account()
    assert snap.unrealized_pnl == 0.0


def test_record_performance_persists_unrealized(tmp_path):
    db = DB(str(tmp_path / "p.db"))
    db.record_performance(day_pnl=10.0, total_assets=1000.0, cash=900.0,
                          gross_exposure=100.0, unrealized_pnl=42.5)
    row = db._conn.execute(
        "SELECT day_pnl, unrealized_pnl FROM performance").fetchone()
    assert row[0] == 10.0 and row[1] == 42.5
    db.close()


def test_record_performance_unrealized_defaults_zero(tmp_path):
    db = DB(str(tmp_path / "p.db"))
    db.record_performance(day_pnl=0.0, total_assets=1.0, cash=1.0, gross_exposure=0.0)
    row = db._conn.execute("SELECT unrealized_pnl FROM performance").fetchone()
    assert row[0] == 0.0
    db.close()
