"""EODReporter reads the SQLite projection (no SDK, no broker) and posts a
Slack summary. Tests seed the DB directly with controlled dates, inject a fake
HTTP poster, and drive with a fixed NY datetime."""
from datetime import datetime
from zoneinfo import ZoneInfo

from autotrader.db import DB
from autotrader.reporting.eod_reporter import EODReporter, ReportData, StrategyGroup

_NY = ZoneInfo("America/New_York")
_DAY = "2026-06-16"


def _now():
    return datetime(2026, 6, 16, 16, 30, tzinfo=_NY)


def _seed(db, *, with_signal=True):
    db._conn.execute(
        "INSERT INTO performance (date,day_pnl,total_assets,cash,gross_exposure,unrealized_pnl,updated_at) "
        "VALUES (?,?,?,?,?,?,?)",
        (_DAY, 842.13, 104712.0, 38204.0, 66000.0, 0.0, _DAY + "T20:30:00+00:00"))
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
    # Covered-call overlay on CLOV (stock leg + short call) for grouped-report tests.
    db._conn.execute(
        "INSERT INTO fills (fill_id,ts,symbol,side,qty,price) VALUES (?,?,?,?,?,?)",
        ("c1", _DAY + "T14:10:00+00:00", "US.CLOV", "BUY", 200, 4.99))
    db._conn.execute(
        "INSERT INTO fills (fill_id,ts,symbol,side,qty,price) VALUES (?,?,?,?,?,?)",
        ("c2", _DAY + "T14:11:00+00:00", "US.CLOV260821C7000", "SELL", 2, 0.20))
    db._conn.execute(
        "INSERT INTO signals (ts,symbol,direction,confidence,rationale,signal_id) "
        "VALUES (?,?,?,?,?,?)",
        (_DAY + "T14:09:00+00:00", "US.CLOV", "SELL", 0.71,
         "COVERED_CALL: ticker sweep score 71", "sig-cc"))
    db._conn.commit()


def _reporter(db, post):
    return EODReporter(db=db, webhook_url="https://hooks.slack.test/x",
                       trading_env="PAPER", http_post=post,
                       retries=3, backoff=lambda s: None)


def test_gather_groups_legs_and_links_signal(tmp_path):
    db = DB(str(tmp_path / "r.db"))
    _seed(db)
    r = _reporter(db, lambda url, payload: 200)
    data = r._gather(_now())
    groups = {g.underlying: g for g in data.groups}
    cc = groups["US.CLOV"]
    assert cc.label == "Covered Call"
    assert {l.side for l in cc.legs} == {"BUY", "SELL"}
    assert abs(cc.economics.net_premium - 40.0) < 1e-6
    assert cc.economics.cap == 7.0
    assert cc.signal is not None and abs(cc.signal.confidence - 0.71) < 1e-9
    # rationale's overlay prefix is stripped for the thesis text
    assert cc.signal.rationale == "ticker sweep score 71"
    assert data.realized_pnl == 842.13
    db.close()


def _aapl_group(data):
    return next(g for g in data.groups if g.underlying == "US.AAPL")


def test_gather_handles_missing_signal(tmp_path):
    db = DB(str(tmp_path / "r.db"))
    _seed(db, with_signal=False)
    r = _reporter(db, lambda url, payload: 200)
    g = _aapl_group(r._gather(_now()))
    assert g.signal is None and g.driver is None


def test_gather_links_rebalance_driver_when_no_signal(tmp_path):
    db = DB(str(tmp_path / "r.db"))
    _seed(db, with_signal=False)
    db._conn.execute(
        "INSERT INTO drivers (ts,symbol,side,kind,detail) VALUES (?,?,?,?,?)",
        (_DAY + "T13:58:00+00:00", "US.AAPL", "BUY", "rebalance",
         "rbal-2026-06-16 · underweight → top-up"))
    db._conn.commit()
    g = _aapl_group(_reporter(db, lambda u, p: 200)._gather(_now()))
    assert g.signal is None and g.driver is not None
    assert g.driver.kind == "rebalance" and "underweight → top-up" in g.driver.detail


def test_signal_takes_precedence_over_rebalance_driver(tmp_path):
    db = DB(str(tmp_path / "r.db"))
    _seed(db, with_signal=True)
    db._conn.execute(
        "INSERT INTO drivers (ts,symbol,side,kind,detail) VALUES (?,?,?,?,?)",
        (_DAY + "T13:58:00+00:00", "US.AAPL", "BUY", "rebalance",
         "rbal-2026-06-16 · underweight → top-up"))
    db._conn.commit()
    g = _aapl_group(_reporter(db, lambda u, p: 200)._gather(_now()))
    assert g.signal is not None and g.driver is None


def test_render_shows_rebalance_driver(tmp_path):
    db = DB(str(tmp_path / "r.db"))
    _seed(db, with_signal=False)
    db._conn.execute(
        "INSERT INTO drivers (ts,symbol,side,kind,detail) VALUES (?,?,?,?,?)",
        (_DAY + "T13:58:00+00:00", "US.AAPL", "BUY", "rebalance",
         "rbal-2026-06-16 · underweight → top-up"))
    db._conn.commit()
    text = _reporter(db, lambda u, p: 200)._render(
        _reporter(db, lambda u, p: 200)._gather(_now()))["text"]
    assert "rebalance" in text and "underweight → top-up" in text
    db.close()


def test_render_enriched_report(tmp_path):
    db = DB(str(tmp_path / "r.db"))
    _seed(db)
    payload = _reporter(db, lambda u, p: 200)._render(
        _reporter(db, lambda u, p: 200)._gather(_now()))
    text = payload["text"]
    assert "Tue, Jun 16 2026" in text and "PAPER" in text
    assert "Realized" in text and "+842.13" in text
    assert "Unrealized" in text
    assert "Premium collected" in text and "Net cash deployed" in text
    assert "Covered Call" in text and "CLOV" in text
    assert "7.00" in text                       # the call strike
    assert "momentum breakout" in text or "ticker sweep score 71" in text
    assert isinstance(payload["blocks"], list) and payload["blocks"][0]["type"] == "header"
    db.close()


def test_render_quiet_day_heartbeat(tmp_path):
    db = DB(str(tmp_path / "r.db"))
    db._conn.execute(
        "INSERT INTO performance "
        "(date,day_pnl,total_assets,cash,gross_exposure,unrealized_pnl,updated_at) "
        "VALUES (?,?,?,?,?,?,?)",
        (_DAY, 0.0, 100000.0, 100000.0, 0.0, 0.0, _DAY + "T20:30:00+00:00"))
    db._conn.commit()
    payload = _reporter(db, lambda u, p: 200)._render(
        _reporter(db, lambda u, p: 200)._gather(_now()))
    assert "no trades today" in payload["text"].lower()
    assert "100,000" in payload["text"]
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


def test_send_build_failure_does_not_raise(tmp_path):
    db = DB(str(tmp_path / "r.db"))
    cap = _Capture(status=200)
    r = _reporter(db, cap)
    def _boom(now):
        raise RuntimeError("db gone")
    r._gather = _boom            # force the build phase to fail
    r.send_eod_report(_now())    # must NOT raise
    assert len(cap.calls) == 0   # never reached the POST
    db.close()


def test_malformed_fill_renders_without_raising(tmp_path):
    """An unparseable option-like symbol must not drop or raise — it is treated
    as a stock leg under its own symbol and appears in the rendered output."""
    db = DB(str(tmp_path / "r.db"))
    db._conn.execute(
        "INSERT INTO performance (date,day_pnl,total_assets,cash,gross_exposure,unrealized_pnl,updated_at) "
        "VALUES (?,?,?,?,?,?,?)",
        (_DAY, 0.0, 100000.0, 100000.0, 0.0, 0.0, _DAY + "T20:30:00+00:00"))
    # 26XX is an invalid date (month 33) — parse_option_code returns None for this symbol.
    malformed = "US.AAPL26XXC1000"
    db._conn.execute(
        "INSERT INTO fills (fill_id,ts,symbol,side,qty,price) VALUES (?,?,?,?,?,?)",
        ("m1", _DAY + "T14:00:00+00:00", malformed, "BUY", 5, 10.00))
    db._conn.commit()
    r = _reporter(db, lambda url, payload: 200)
    # Must not raise
    payload = r._render(r._gather(_now()))
    # The fill must appear in the output (as symbol or underlying)
    assert malformed in payload["text"] or "AAPL26XXC1000" in payload["text"]
    db.close()


def _seed_null_gross(db):
    db._conn.execute(
        "INSERT INTO performance "
        "(date,day_pnl,total_assets,cash,gross_exposure,unrealized_pnl,updated_at) "
        "VALUES (?,?,?,?,?,?,?)",
        (_DAY, 12.0, 100000.0, 100000.0, None, 0.0, _DAY + "T20:30:00+00:00"))
    db._conn.commit()


def test_render_null_gross_shows_unavailable(tmp_path):
    db = DB(str(tmp_path / "r.db"))
    _seed_null_gross(db)
    payload = _reporter(db, lambda u, p: 200)._render(
        _reporter(db, lambda u, p: 200)._gather(_now()))
    text = payload["text"]
    assert "exposure unavailable — snapshot stale" in text
    assert "Gross exp 0%" not in text
    # Block Kit gross field carries the same message, never "0%".
    flat = " ".join(
        f["text"] for b in payload["blocks"] if b.get("fields")
        for f in b["fields"])
    assert "exposure unavailable — snapshot stale" in flat
    db.close()


def test_render_realized_dash_when_null(tmp_path):
    db = DB(str(tmp_path / "r.db"))
    db._conn.execute(
        "INSERT INTO performance "
        "(date,day_pnl,total_assets,cash,gross_exposure,unrealized_pnl,updated_at) "
        "VALUES (?,?,?,?,?,?,?)",
        (_DAY, None, 100000.0, 100000.0, 66000.0, 0.0, _DAY + "T20:30:00+00:00"))
    db._conn.commit()
    text = _reporter(db, lambda u, p: 200)._render(
        _reporter(db, lambda u, p: 200)._gather(_now()))["text"]
    assert "Realized —" in text
    assert "Realized +0.00" not in text            # no defaulted realized 0.00


def test_render_unrealized_unavailable_when_stale(tmp_path):
    db = DB(str(tmp_path / "r.db"))
    db._conn.execute(
        "INSERT INTO performance "
        "(date,day_pnl,total_assets,cash,gross_exposure,unrealized_pnl,updated_at) "
        "VALUES (?,?,?,?,?,?,?)",
        (_DAY, 5.0, 100000.0, 100000.0, None, 0.0, _DAY + "T20:30:00+00:00"))
    db._conn.commit()
    text = _reporter(db, lambda u, p: 200)._render(
        _reporter(db, lambda u, p: 200)._gather(_now()))["text"]
    assert "Unrealized unavailable" in text
