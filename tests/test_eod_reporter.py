"""EODReporter reads the SQLite projection (no SDK, no broker) and posts a
Slack summary. Tests seed the DB directly with controlled dates, inject a fake
HTTP poster, and drive with a fixed NY datetime."""
from datetime import datetime
from zoneinfo import ZoneInfo

from autotrader.db import DB
from autotrader.reporting.eod_reporter import EODReporter

_NY = ZoneInfo("America/New_York")
_DAY = "2026-06-16"


def _now():
    return datetime(2026, 6, 16, 16, 30, tzinfo=_NY)


def _seed(db, *, with_signal=True):
    db._conn.execute(
        "INSERT INTO performance (date,day_pnl,total_assets,cash,gross_exposure,updated_at) "
        "VALUES (?,?,?,?,?,?)",
        (_DAY, 842.13, 104712.0, 38204.0, 66000.0, _DAY + "T20:30:00+00:00"))
    db._conn.execute(
        "INSERT INTO fills (fill_id,ts,symbol,side,qty,price) VALUES (?,?,?,?,?,?)",
        ("f1", _DAY + "T14:00:00+00:00", "US.AAPL", "BUY", 10, 198.00))
    db._conn.execute(
        "INSERT INTO fills (fill_id,ts,symbol,side,qty,price) VALUES (?,?,?,?,?,?)",
        ("f2", _DAY + "T14:05:00+00:00", "US.AAPL", "BUY", 6, 199.00))
    if with_signal:
        db._conn.execute(
            "INSERT INTO signals (ts,symbol,direction,confidence,rationale,signal_id) "
            "VALUES (?,?,?,?,?,?)",
            (_DAY + "T13:59:00+00:00", "US.AAPL", "BUY", 0.82, "momentum breakout", "sig-1"))
    db._conn.execute(
        "INSERT INTO positions (symbol,qty,avg_price,updated_at) VALUES (?,?,?,?)",
        ("US.AAPL", 16, 198.40, _DAY + "T20:30:00+00:00"))
    db._conn.commit()


def _reporter(db, post):
    return EODReporter(db=db, webhook_url="https://hooks.slack.test/x",
                       trading_env="PAPER", http_post=post,
                       retries=3, backoff=lambda s: None)


def test_gather_aggregates_fills_and_links_signal(tmp_path):
    db = DB(str(tmp_path / "r.db"))
    _seed(db)
    r = _reporter(db, lambda url, payload: 200)
    data = r._gather(_now())
    assert len(data.activity) == 1
    line = data.activity[0]
    assert line.side == "BUY" and line.symbol == "US.AAPL"
    assert line.qty == 16                      # 10 + 6 summed
    assert abs(line.avg_price - 198.375) < 1e-6  # qty-weighted: (10*198 + 6*199)/16
    assert line.signal is not None
    assert abs(line.signal.confidence - 0.82) < 1e-9
    assert line.signal.rationale == "momentum breakout"
    assert data.positions == (("US.AAPL", 16),)
    assert data.day_pnl == 842.13 and data.total_assets == 104712.0
    db.close()


def test_gather_handles_missing_signal(tmp_path):
    db = DB(str(tmp_path / "r.db"))
    _seed(db, with_signal=False)
    r = _reporter(db, lambda url, payload: 200)
    data = r._gather(_now())
    assert len(data.activity) == 1
    assert data.activity[0].signal is None
    db.close()
