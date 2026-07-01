"""Offline tests for MoomooBroker logic that does NOT need OpenD/the SDK.

We bypass __init__ (which imports the vendored common.py and requires a live
OpenD) via __new__, then inject a fake `common` module and a fake trade context.
This lets us pin the safety-critical staleness behaviour of get_account without
a broker connection. The happy path is covered by the RUN_LIVE live tests.
"""
import pytest

from autotrader.domain import OrderState
from autotrader.moomoo_broker import MoomooBroker


class _Row(dict):
    """A dict that also supports the .get used by the fake helpers."""


class _DF:
    """Minimal pandas-like frame over a list of _Row dicts."""
    def __init__(self, rows):
        self._rows = rows

    class _ILoc:
        def __init__(self, rows):
            self._rows = rows

        def __getitem__(self, i):
            return self._rows[i]

    @property
    def iloc(self):
        return self._ILoc(self._rows)

    def __len__(self):
        return len(self._rows)


class _FakeCommon:
    """Stand-in for the vendored common.py surface MoomooBroker uses."""
    RET_OK = 0

    def __init__(self, env="SIMULATE"):
        self._env_name = env

    def get_default_trd_env(self):
        return self._env_name

    def is_empty(self, data):
        return data is None or len(data) == 0

    def safe_get(self, row, *keys, default=""):
        for k in keys:
            if k in row and row[k] is not None:
                return row[k]
        return default

    def safe_float(self, v, default=0.0):
        try:
            return float(v)
        except (TypeError, ValueError):
            return default

    def safe_int(self, v, default=0):
        try:
            return int(v)
        except (TypeError, ValueError):
            return default

    def format_enum(self, v):
        return getattr(v, "name", str(v))


class _FakeTrade:
    """Fake trade context: account query OK, but position query FAILS."""
    def __init__(self, *, acc_ok=True, pos_ok=True, total=5000.0):
        self._acc_ok = acc_ok
        self._pos_ok = pos_ok
        self._total = total

    def accinfo_query(self, **kw):
        if not self._acc_ok:
            return 1, "accinfo error"
        return 0, _DF([_Row(cash=1000.0, total_assets=self._total, realized_pl=0.0)])

    def position_list_query(self, **kw):
        if not self._pos_ok:
            return 1, "position query error"  # non-OK ret
        return 0, _DF([])  # genuinely flat


def _broker(trade):
    from autotrader.rate_limiter import RateLimiter
    b = MoomooBroker.__new__(MoomooBroker)  # bypass __init__ (no OpenD needed)
    b._c = _FakeCommon()
    b._trade = trade
    b._quote = None
    b._acc_id = 1
    b._order_rl = RateLimiter(capacity=15.0, refill_rate=0.5)
    b._refresh_rl = RateLimiter(capacity=10.0, refill_rate=10 / 30)
    return b


def test_get_account_is_stale_when_position_query_fails():
    # total_assets > 0 but the position query errors: the snapshot must be marked
    # stale (not falsely flat) so the risk core refuses to trade.
    b = _broker(_FakeTrade(acc_ok=True, pos_ok=False, total=5000.0))
    snap = b.get_account()
    assert snap.stale is True
    assert snap.positions == ()


def test_get_account_not_stale_when_flat_with_assets():
    # Position query OK and empty, with assets present: a genuine flat account is
    # NOT stale.
    b = _broker(_FakeTrade(acc_ok=True, pos_ok=True, total=5000.0))
    snap = b.get_account()
    assert snap.stale is False


def test_get_account_raises_when_accinfo_fails():
    from autotrader.domain import BrokerError
    b = _broker(_FakeTrade(acc_ok=False))
    with pytest.raises(BrokerError):
        b.get_account()


class _CapturingTrade:
    """Records kwargs passed to deal_list_query for inspection (LIVE deal feed)."""
    def __init__(self):
        self.captured = {}

    def deal_list_query(self, **kwargs):
        self.captured.update(kwargs)
        return 0, None  # RET_OK=0, empty data -> reconcile returns []


class _OrderListTrade:
    """Fake for the PAPER fill-synthesis path: the deal feed is unsupported (mirrors
    Moomoo's 'Paper trading does not support deal data'), and order_list_query
    returns the given order rows."""
    def __init__(self, order_rows):
        self._order_rows = order_rows
        self.deal_called = False

    def deal_list_query(self, **kwargs):
        self.deal_called = True
        return -1, "Paper trading does not support deal data."

    def order_list_query(self, **kwargs):
        return 0, _DF(self._order_rows)


def _broker_with_trade(trade, env="SIMULATE"):
    """Extend the existing _broker() helper with a specific trade context + env."""
    from autotrader.rate_limiter import RateLimiter
    b = MoomooBroker.__new__(MoomooBroker)
    b._c = _FakeCommon(env)
    b._trade = trade
    b._quote = None
    b._acc_id = 1
    b._order_rl = RateLimiter(capacity=15.0, refill_rate=0.5)
    b._refresh_rl = RateLimiter(capacity=10.0, refill_rate=10 / 30)
    return b


def test_reconcile_fills_since_passes_begin_time():
    """LIVE: reconcile_fills(since=...) must forward begin_time to deal_list_query."""
    trade = _CapturingTrade()
    b = _broker_with_trade(trade, env="REAL")
    b.reconcile_fills(since="2026-06-12 09:30:00")
    assert "begin_time" in trade.captured, "begin_time must be forwarded when since is set"
    assert trade.captured["begin_time"] == "2026-06-12 09:30:00"


def test_reconcile_fills_no_since_omits_begin_time():
    """LIVE: reconcile_fills(since=None) must NOT pass begin_time to deal_list_query."""
    trade = _CapturingTrade()
    b = _broker_with_trade(trade, env="REAL")
    b.reconcile_fills(since=None)
    assert "begin_time" not in trade.captured, "begin_time must be absent when since=None"


def test_reconcile_fills_paper_synthesizes_from_filled_orders():
    """PAPER: the deal feed is unsupported, so fills are reconstructed from the order
    list. One fill per order with dealt_qty > 0 (FILLED_ALL or FILLED_PART);
    unfilled/rejected orders produce none."""
    rows = [
        _Row(order_id="810543", code="US.SCHF", trd_side="BUY", order_status="FILLED_ALL",
             dealt_qty=97, dealt_avg_price=28.30, updated_time="2026-06-17 10:38:52"),
        _Row(order_id="810545", code="US.O", trd_side="BUY", order_status="FILLED_PART",
             dealt_qty=40, dealt_avg_price=61.59, updated_time="2026-06-17 10:39:40"),
        _Row(order_id="810840", code="US.MSFT", trd_side="BUY", order_status="SUBMITTED",
             dealt_qty=0, dealt_avg_price=0, updated_time="2026-06-17 14:10:00"),
        _Row(order_id="810999", code="US.MSFT", trd_side="SELL", order_status="FAILED",
             dealt_qty=0, dealt_avg_price=0, updated_time="2026-06-17 14:10:31"),
    ]
    b = _broker_with_trade(_OrderListTrade(rows), env="SIMULATE")
    fills = b.reconcile_fills(since=None)
    by = {f.symbol: f for f in fills}
    assert set(by) == {"US.SCHF", "US.O"}                  # unfilled + rejected dropped
    assert by["US.SCHF"].fill_id == "paper-810543"          # stable id -> idempotent
    assert by["US.SCHF"].side == "BUY" and by["US.SCHF"].qty == 97
    assert by["US.SCHF"].price == 28.30
    assert by["US.SCHF"].ts.startswith("2026-06-17")
    assert by["US.O"].qty == 40                             # partial fill captured


def test_reconcile_fills_paper_never_relies_on_deal_feed():
    """PAPER: must not depend on the unsupported deal feed even when no orders exist."""
    trade = _OrderListTrade([])
    b = _broker_with_trade(trade, env="SIMULATE")
    assert b.reconcile_fills(since=None) == []
    assert trade.deal_called is False


def test_place_order_raises_rate_limit_when_order_limiter_drained():
    """place_order must raise BrokerError(RATE_LIMIT) if the order bucket is empty."""
    from autotrader.domain import BrokerError, BrokerErrorKind, OrderRequest
    from autotrader.rate_limiter import RateLimiter

    class _AlwaysTimeout:
        def acquire(self, timeout=60.0):
            return False  # simulates an always-drained bucket

    b = MoomooBroker.__new__(MoomooBroker)
    b._order_rl = _AlwaysTimeout()
    b._refresh_rl = RateLimiter(capacity=10, refill_rate=10 / 30)

    req = OrderRequest(symbol="US.AAPL", side="BUY", qty=1,
                       order_type="MARKET", limit_price=None,
                       client_order_id="test-cid")
    with pytest.raises(BrokerError) as exc_info:
        b.place_order(req)
    assert exc_info.value.kind == BrokerErrorKind.RATE_LIMIT


def test_get_account_marks_positions_not_loaded_on_query_failure(monkeypatch):
    b = _broker(_FakeTrade(acc_ok=True, pos_ok=True))
    monkeypatch.setattr(b, "_positions", lambda: None)
    snap = b.get_account()
    assert snap.positions_loaded is False
    assert snap.stale is True
    assert snap.positions == ()


def test_get_account_marks_positions_loaded_when_flat(monkeypatch):
    b = _broker(_FakeTrade(acc_ok=True, pos_ok=True))
    monkeypatch.setattr(b, "_positions", lambda: [])
    snap = b.get_account()
    assert snap.positions_loaded is True
