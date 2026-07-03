"""C6: in SHARED mode AutoTrader must never cancel, stop-manage, flatten, or
ingest the SNP bot's orders/positions/fills."""
from datetime import date

import pytest

from autotrader.config import RiskConfig
from autotrader.db import DB
from autotrader.domain import Fill, OrderRequest, Position
from autotrader.lifecycle import cancel_tracked_orders, ground_truth_sync
from autotrader.main import TradeEngine
from autotrader.sim_broker import SimBroker
from autotrader.stops import StopManager
from autotrader.strategies.breakout import BreakoutParams, BreakoutStrategy


def _cfg(ownership="SHARED", allowed_symbols=frozenset()):
    return RiskConfig(trading_env="PAPER", min_confidence=0.5, max_order_notional=1e6,
                      max_position_qty=1000, daily_loss_limit=500,
                      max_gross_exposure=1e6, allowed_symbols=allowed_symbols,
                      trailing_stop_pct=5.0, account_ownership=ownership)


def _seed_foreign_order(broker, cid="snp-order-1"):
    return broker.place_order(OrderRequest(symbol="US.SNP", side="BUY", qty=5,
                                           order_type="TRAILING_STOP",  # rests
                                           limit_price=None, client_order_id=cid,
                                           trail_percent=5.0))


def test_cancel_tracked_leaves_foreign_orders(tmp_path):
    db = DB(str(tmp_path / "t.db"))
    b = SimBroker({"US.SNP": 50.0, "US.MARA": 20.0})
    foreign = _seed_foreign_order(b)
    mine = b.place_order(OrderRequest(symbol="US.MARA", side="SELL", qty=1,
                                      order_type="TRAILING_STOP", limit_price=None,
                                      client_order_id="at-mine", trail_percent=5.0))
    db.record_trade(client_order_id="at-mine", symbol="US.MARA", side="SELL", qty=1,
                    order_type="TRAILING_STOP", limit_price=None,
                    broker_order_id=mine.broker_order_id, state="SUBMITTED")
    assert cancel_tracked_orders(b, db) == 1
    remaining = {o.broker_order_id for o in b.get_open_orders()}
    assert foreign.broker_order_id in remaining          # SNP order untouched
    assert mine.broker_order_id not in remaining


def test_cancel_tracked_unknown_book_is_noop(tmp_path):
    db = DB(str(tmp_path / "t.db"))
    b = SimBroker({"US.MARA": 20.0})
    b.fail_open_orders = True
    assert cancel_tracked_orders(b, db) == 0


def test_stop_reconcile_shared_ignores_foreign(tmp_path):
    db = DB(str(tmp_path / "t.db"))
    b = SimBroker({"US.SNP": 50.0, "US.MARA": 20.0})
    foreign = _seed_foreign_order(b)
    b._positions["US.SNP"] = Position("US.SNP", 5, 48.0)   # SNP's position

    class _Eng:
        attached = []
        def attach_trailing_stop(self, symbol, qty, price, tag):
            self.attached.append(symbol)
            return True

    sm = StopManager(_Eng(), b, db, _cfg("SHARED"))
    sm.reconcile(date(2026, 7, 6))
    assert foreign.broker_order_id in {o.broker_order_id for o in b.get_open_orders()}
    assert "US.SNP" not in _Eng.attached                 # no stop on SNP's position


def test_stop_reconcile_shared_attaches_owned_alongside_foreign(tmp_path):
    """Positive-case companion to test_stop_reconcile_shared_ignores_foreign:
    an OWNED position (US.MARA, has a trades row) held simultaneously with a
    FOREIGN position (US.SNP, no trades row) must still get its trailing stop
    attached in SHARED mode — the scoping must discriminate, not just exclude
    everything foreign."""
    db = DB(str(tmp_path / "t.db"))
    b = SimBroker({"US.SNP": 50.0, "US.MARA": 20.0})
    b._positions["US.SNP"] = Position("US.SNP", 5, 48.0)     # foreign position
    b._positions["US.MARA"] = Position("US.MARA", 10, 19.0)  # our position
    db.record_trade(client_order_id="at-seed", symbol="US.MARA", side="BUY", qty=10,
                    order_type="MARKET", limit_price=None,
                    broker_order_id="sim-seed", state="FILLED")

    class _Eng:
        attached = []
        def attach_trailing_stop(self, symbol, qty, price, tag):
            self.attached.append(symbol)
            return True

    sm = StopManager(_Eng(), b, db, _cfg("SHARED"))
    result = sm.reconcile(date(2026, 7, 6))
    assert "US.MARA" in _Eng.attached           # owned position gets a stop
    assert "US.SNP" not in _Eng.attached        # foreign position does not
    assert result.attached == 1


def test_stop_reconcile_sole_still_cancels_unrecognized(tmp_path):
    """SOLE-mode regression guard: an order with no trades row is still an
    orphan and gets cancelled — SHARED's foreign-order carve-out must not leak
    into the default mode."""
    db = DB(str(tmp_path / "t.db"))
    b = SimBroker({"US.SNP": 50.0})
    foreign = _seed_foreign_order(b)

    class _Eng:
        def attach_trailing_stop(self, symbol, qty, price, tag):
            return True

    sm = StopManager(_Eng(), b, db, _cfg("SOLE"))
    sm.reconcile(date(2026, 7, 6))
    assert foreign.broker_order_id not in {o.broker_order_id for o in b.get_open_orders()}


def test_ground_truth_sync_owned_only_filters_foreign_fills(tmp_path):
    db = DB(str(tmp_path / "t.db"))
    b = SimBroker({"US.MARA": 20.0})
    b._fills.append(Fill(fill_id="f-snp", symbol="US.SNP", side="BUY", qty=5,
                         price=50.0, ts="t1", client_order_id="snp-1"))
    b._fills.append(Fill(fill_id="f-me", symbol="US.MARA", side="BUY", qty=1,
                         price=20.0, ts="t2", client_order_id="at-abc"))
    ground_truth_sync(b, db, owned_only=True)
    rows = db._conn.execute("SELECT symbol FROM fills").fetchall()
    assert {r[0] for r in rows} == {"US.MARA"}           # SNP fill never ingested


def test_ground_truth_sync_owned_only_filters_foreign_positions(tmp_path):
    db = DB(str(tmp_path / "t.db"))
    b = SimBroker({"US.MARA": 20.0, "US.SNP": 50.0})
    # Position we've traded before (owned via a prior trades row).
    db.record_trade(client_order_id="at-old", symbol="US.MARA", side="BUY", qty=1,
                    order_type="MARKET", limit_price=None,
                    broker_order_id="sim-old", state="FILLED")
    b._positions["US.MARA"] = Position("US.MARA", 1, 20.0)
    b._positions["US.SNP"] = Position("US.SNP", 5, 50.0)   # foreign, never traded by us
    ground_truth_sync(b, db, owned_only=True)
    rows = {r[0] for r in db._conn.execute("SELECT symbol FROM positions").fetchall()}
    assert rows == {"US.MARA"}


def test_ground_truth_sync_sole_preserves_partial_failure_order(tmp_path):
    """Finding 2 regression guard: before V11, ground_truth_sync replaced
    positions BEFORE fetching fills. That order must be preserved exactly in
    SOLE mode (owned_only=False, the default) — so if db.record_fills raises,
    the position replacement is already committed, matching pre-Task-17
    partial-failure semantics (git show 81ed89a:autotrader/lifecycle.py). If
    the reorder introduced for SHARED mode ever leaked into SOLE mode, fills
    would be fetched/recorded first and a raise there would leave positions
    untouched — this test would then fail."""
    db = DB(str(tmp_path / "t.db"))
    b = SimBroker({"US.MARA": 20.0})
    b._positions["US.MARA"] = Position("US.MARA", 10, 19.0)

    def _boom(fills):
        raise RuntimeError("simulated fills failure")
    db.record_fills = _boom

    with pytest.raises(RuntimeError):
        ground_truth_sync(b, db)  # owned_only defaults to False (SOLE)

    rows = {r[0] for r in db._conn.execute("SELECT symbol FROM positions").fetchall()}
    assert rows == {"US.MARA"}   # positions already committed before fills raised


def test_ground_truth_sync_sole_ingests_everything(tmp_path):
    """SOLE-mode regression guard (default owned_only=False)."""
    db = DB(str(tmp_path / "t.db"))
    b = SimBroker({"US.MARA": 20.0})
    b._fills.append(Fill(fill_id="f-snp", symbol="US.SNP", side="BUY", qty=5,
                         price=50.0, ts="t1", client_order_id="snp-1"))
    ground_truth_sync(b, db)
    rows = db._conn.execute("SELECT symbol FROM fills").fetchall()
    assert {r[0] for r in rows} == {"US.SNP"}


def test_flatten_all_shared_skips_foreign_position(tmp_path):
    db = DB(str(tmp_path / "t.db"))
    b = SimBroker({"US.MARA": 20.0, "US.SNP": 50.0}, cash=100000.0)
    b._positions["US.MARA"] = Position("US.MARA", 10, 19.0)   # ours
    b._positions["US.SNP"] = Position("US.SNP", 5, 48.0)      # foreign
    db.record_trade(client_order_id="at-seed", symbol="US.MARA", side="BUY", qty=10,
                    order_type="MARKET", limit_price=None,
                    broker_order_id="sim-seed", state="FILLED")
    strat = BreakoutStrategy(BreakoutParams(symbol="US.MARA", stop_loss_pct=0.05,
                                            take_profit_pct=0.10, confidence=0.9))
    eng = TradeEngine(b, strat, _cfg("SHARED", allowed_symbols=frozenset({"US.MARA"})),
                      order_qty=1, audit_path=str(tmp_path / "a.jsonl"), db=db)
    eng._flatten_all(b.get_account(), "halt-test")
    filled_symbols = {f.symbol for f in b._fills if f.side == "SELL"}
    assert filled_symbols == {"US.MARA"}
    assert b.get_account().position_qty("US.SNP") == 5   # foreign position untouched
