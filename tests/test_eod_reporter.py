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


def test_render_full_report_text_and_blocks(tmp_path):
    db = DB(str(tmp_path / "r.db"))
    _seed(db)
    r = _reporter(db, lambda url, payload: 200)
    payload = r._render(r._gather(_now()))
    text = payload["text"]
    assert "Tue, Jun 16 2026" in text and "PAPER" in text
    assert "+842.13" in text                  # day P&L, signed
    assert "BUY US.AAPL" in text and "16" in text
    assert "198.38" in text or "198.37" in text  # weighted avg, 2dp
    assert "momentum breakout" in text and "0.82" in text
    assert "US.AAPL 16" in text               # open positions
    assert isinstance(payload["blocks"], list) and len(payload["blocks"]) >= 3
    assert payload["blocks"][0]["type"] == "header"
    db.close()


def test_render_quiet_day_heartbeat(tmp_path):
    db = DB(str(tmp_path / "r.db"))
    db._conn.execute(
        "INSERT INTO performance (date,day_pnl,total_assets,cash,gross_exposure,updated_at) "
        "VALUES (?,?,?,?,?,?)",
        (_DAY, 0.0, 100000.0, 100000.0, 0.0, _DAY + "T20:30:00+00:00"))
    db._conn.commit()
    r = _reporter(db, lambda url, payload: 200)
    payload = r._render(r._gather(_now()))
    assert "no trades today" in payload["text"].lower()
    assert "100,000" in payload["text"]       # P&L header still present
    db.close()


class _Capture:
    """Fake http_post: records calls, fails the first `fail_times`, else returns status."""
    def __init__(self, status=200, fail_times=0):
        self.calls = []
        self._status = status
        self._fail_times = fail_times

    def __call__(self, url, payload):
        self.calls.append((url, payload))
        if len(self.calls) <= self._fail_times:
            raise OSError("boom")
        return self._status


def test_send_posts_once_on_success(tmp_path):
    db = DB(str(tmp_path / "r.db"))
    _seed(db)
    cap = _Capture(status=200)
    _reporter(db, cap).send_eod_report(_now())
    assert len(cap.calls) == 1
    assert cap.calls[0][0] == "https://hooks.slack.test/x"
    assert "blocks" in cap.calls[0][1]
    db.close()


def test_send_retries_then_succeeds(tmp_path):
    db = DB(str(tmp_path / "r.db"))
    _seed(db)
    cap = _Capture(status=200, fail_times=1)
    _reporter(db, cap).send_eod_report(_now())
    assert len(cap.calls) == 2          # one failure, one success
    db.close()


def test_send_failure_exhausts_retries_without_raising(tmp_path):
    db = DB(str(tmp_path / "r.db"))
    _seed(db)
    cap = _Capture(fail_times=99)
    _reporter(db, cap).send_eod_report(_now())   # must NOT raise
    assert len(cap.calls) == 3          # retries=3
    db.close()


def test_send_non_2xx_status_is_retried(tmp_path):
    db = DB(str(tmp_path / "r.db"))
    _seed(db)
    cap = _Capture(status=500)
    _reporter(db, cap).send_eod_report(_now())   # must NOT raise
    assert len(cap.calls) == 3
    db.close()
