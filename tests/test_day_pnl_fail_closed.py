from autotrader.config import RiskConfig
from autotrader.domain import AccountSnapshot
from autotrader.risk_check import evaluate, RiskAction
from tests.test_moomoo_broker_offline import _DF, _Row, _broker


def _cfg():
    return RiskConfig(trading_env="PAPER", min_confidence=0.5,
                      max_order_notional=1e6, max_position_qty=1000,
                      daily_loss_limit=500, max_gross_exposure=1e6,
                      allowed_symbols=frozenset())


def _snap(**kw):
    base = dict(cash=1000.0, total_assets=1000.0, day_pnl=0.0, stale=False)
    base.update(kw)
    return AccountSnapshot(**base)


def test_unknown_day_pnl_gates_entries():
    assert evaluate(_snap(day_pnl_known=False), _cfg()) is RiskAction.GATE


def test_known_zero_pnl_ok():
    assert evaluate(_snap(day_pnl_known=True), _cfg()) is RiskAction.OK


def test_unknown_never_halts_even_when_zero():
    # fail CLOSED means block entries — never flatten off unknown data
    assert evaluate(_snap(day_pnl_known=False), _cfg()) is not RiskAction.HALT


# --- MoomooBroker.get_account field-mapping (realistic broker field-shapes) ---

class _PnlTrade:
    """Fake trade context with a caller-controlled account row, so we can
    simulate a renamed/missing P&L field on the live API (the exact failure
    this task guards against)."""
    def __init__(self, acc_row, *, pos_ok=True):
        self._acc_row = acc_row
        self._pos_ok = pos_ok

    def accinfo_query(self, **kw):
        return 0, _DF([self._acc_row])

    def position_list_query(self, **kw):
        if not self._pos_ok:
            return 1, "position query error"
        return 0, _DF([])


def test_get_account_day_pnl_known_when_realized_pl_present():
    row = _Row(cash=1000.0, total_assets=5000.0, realized_pl=-42.5)
    b = _broker(_PnlTrade(row))
    snap = b.get_account()
    assert snap.day_pnl_known is True
    assert snap.day_pnl == -42.5


def test_get_account_day_pnl_known_via_fallback_field():
    row = _Row(cash=1000.0, total_assets=5000.0, today_pnl_value=17.0)
    b = _broker(_PnlTrade(row))
    snap = b.get_account()
    assert snap.day_pnl_known is True
    assert snap.day_pnl == 17.0


def test_get_account_day_pnl_unknown_when_field_absent():
    # Neither realized_pl nor today_pnl_value present (e.g. renamed on a live
    # API change) — must fail closed, never silently read as zero P&L.
    row = _Row(cash=1000.0, total_assets=5000.0)
    b = _broker(_PnlTrade(row))
    snap = b.get_account()
    assert snap.day_pnl_known is False
    assert snap.day_pnl == 0.0


def test_get_account_day_pnl_unknown_when_field_non_finite():
    row = _Row(cash=1000.0, total_assets=5000.0, realized_pl=float("nan"))
    b = _broker(_PnlTrade(row))
    snap = b.get_account()
    assert snap.day_pnl_known is False
    assert snap.day_pnl == 0.0


def test_get_account_day_pnl_unknown_propagates_on_position_query_failure():
    # The position-query-FAILED return path is a second, separate
    # AccountSnapshot construction site — day_pnl_known must thread through
    # it too, not just the normal-path return.
    row = _Row(cash=1000.0, total_assets=5000.0)
    b = _broker(_PnlTrade(row, pos_ok=False))
    snap = b.get_account()
    assert snap.positions_loaded is False
    assert snap.day_pnl_known is False
