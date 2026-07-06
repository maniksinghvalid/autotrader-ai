"""Option B: when the broker cannot source day P&L (moomoo paper returns
realized_pl='N/A' -> AccountSnapshot.day_pnl_known False), the engine's risk check
derives it from OUR OWN records instead of failing closed: realized from the fill
ledger (avg-cost, today's sells) + unrealized marked from live quotes. A broker
that DOES report P&L, or the absence of a ledger, is left untouched (still fails
closed)."""
from datetime import datetime, timezone

from autotrader.config import RiskConfig
from autotrader.db import DB
from autotrader.domain import AccountSnapshot, Fill, Position
from autotrader.lifecycle import EntryGate
from autotrader.main import TradeEngine
from autotrader.sim_broker import SimBroker
from autotrader.strategies.threshold import StrategyParams, ThresholdStrategy

NOW = datetime(2026, 7, 6, 15, 0, tzinfo=timezone.utc)
DAY = NOW.date().isoformat()          # "2026-07-06"
PRIOR = "2026-07-03"                   # a prior trading day


class UnknownPnlBroker(SimBroker):
    """Broker that cannot source day P&L (like moomoo paper: realized_pl='N/A')."""
    def __init__(self, quotes, positions=(), **kw):
        super().__init__(quotes, **kw)
        self._snap_positions = tuple(positions)

    def get_account(self):
        base = super().get_account()
        return AccountSnapshot(cash=base.cash, total_assets=base.total_assets,
                               day_pnl=0.0, day_pnl_known=False, stale=False,
                               positions_loaded=True, positions=self._snap_positions)


class KnownPnlBroker(SimBroker):
    """Broker that DOES report day P&L — the engine must not override it."""
    def __init__(self, quotes, day_pnl, **kw):
        super().__init__(quotes, **kw)
        self._day_pnl = day_pnl

    def get_account(self):
        base = super().get_account()
        return AccountSnapshot(cash=base.cash, total_assets=base.total_assets,
                               day_pnl=self._day_pnl, day_pnl_known=True,
                               stale=False, positions=base.positions)


def _cfg(**kw):
    base = dict(trading_env="PAPER", min_confidence=0.6, max_order_notional=1e9,
                max_position_qty=10_000, daily_loss_limit=500,
                max_gross_exposure=1e9, allowed_symbols=frozenset({"US.AAPL"}),
                trailing_stop_pct=5.0, daily_loss_halt=1000)
    base.update(kw)
    return RiskConfig(**base)


def _engine(tmp_path, broker, cfg, gate, *, db="make"):
    strat = ThresholdStrategy(StrategyParams("US.AAPL", 1.0, 0.05, 0.10, 0.7))
    if db == "make":
        db = DB(str(tmp_path / "t.db"))
    eng = TradeEngine(broker, strat, cfg, order_qty=1,
                      audit_path=str(tmp_path / "audit.jsonl"), db=db,
                      entry_gate=gate)
    return eng, db


def _fill(symbol, side, qty, price, ts):
    return Fill(fill_id=f"{symbol}-{side}-{ts}", symbol=symbol, side=side,
                qty=qty, price=price, ts=ts)


def test_realized_loss_from_fills_halts(tmp_path):
    # BUY 100 @100 (prior day), SELL 100 @88 today -> realized -1200 < -1000 halt.
    broker = UnknownPnlBroker({"US.AAPL": 88.0})
    gate = EntryGate(enabled=True)
    eng, db = _engine(tmp_path, broker, _cfg(), gate)
    db.record_fills([_fill("US.AAPL", "BUY", 100, 100.0, f"{PRIOR}T14:00:00"),
                     _fill("US.AAPL", "SELL", 100, 88.0, f"{DAY}T14:00:00")])
    assert eng.apply_risk_check(NOW) == "HALT"
    assert gate.halted is True
    db.close()


def test_realized_loss_from_fills_soft_gates(tmp_path):
    # SELL 100 @94 today -> realized -600: past -500 limit, short of -1000 halt.
    broker = UnknownPnlBroker({"US.AAPL": 94.0})
    gate = EntryGate(enabled=True)
    eng, db = _engine(tmp_path, broker, _cfg(), gate)
    db.record_fills([_fill("US.AAPL", "BUY", 100, 100.0, f"{PRIOR}T14:00:00"),
                     _fill("US.AAPL", "SELL", 100, 94.0, f"{DAY}T14:00:00")])
    assert eng.apply_risk_check(NOW) == "GATE"
    assert gate.entries_enabled is False
    assert gate.halted is False
    db.close()


def test_no_sells_today_is_known_zero_not_fail_closed(tmp_path):
    # Only a BUY today: realized is 0 (nothing closed), which is KNOWN, not
    # UNKNOWN — so the check must return OK, NOT fail-closed GATE.
    broker = UnknownPnlBroker({"US.AAPL": 100.0})
    gate = EntryGate(enabled=True)
    eng, db = _engine(tmp_path, broker, _cfg(), gate)
    db.record_fills([_fill("US.AAPL", "BUY", 10, 100.0, f"{DAY}T14:00:00")])
    assert eng.apply_risk_check(NOW) == "OK"
    assert gate.entries_enabled is True
    db.close()


def test_empty_ledger_is_known_zero_not_fail_closed(tmp_path):
    broker = UnknownPnlBroker({"US.AAPL": 100.0})
    gate = EntryGate(enabled=True)
    eng, db = _engine(tmp_path, broker, _cfg(), gate)
    assert eng.apply_risk_check(NOW) == "OK"
    assert gate.entries_enabled is True
    db.close()


def test_unrealized_marked_from_quotes_gates(tmp_path):
    # No realized (no sells), but an open position marked from a live quote below
    # avg cost breaches the unrealized gate: 100 sh, avg 100, quote 96 -> -400.
    pos = (Position("US.AAPL", 100, 100.0),)
    broker = UnknownPnlBroker({"US.AAPL": 96.0}, positions=pos)
    gate = EntryGate(enabled=True)
    eng, db = _engine(tmp_path, broker, _cfg(unrealized_loss_gate=300.0), gate)
    assert eng.apply_risk_check(NOW) == "GATE"
    assert gate.halted is False
    db.close()


def test_broker_with_known_pnl_not_overridden(tmp_path):
    # Broker reports day_pnl=-100 (OK). A ledger implying a -1200 loss must NOT
    # override it — a knowing broker is authoritative.
    broker = KnownPnlBroker({"US.AAPL": 88.0}, day_pnl=-100.0)
    gate = EntryGate(enabled=True)
    eng, db = _engine(tmp_path, broker, _cfg(), gate)
    db.record_fills([_fill("US.AAPL", "BUY", 100, 100.0, f"{PRIOR}T14:00:00"),
                     _fill("US.AAPL", "SELL", 100, 88.0, f"{DAY}T14:00:00")])
    assert eng.apply_risk_check(NOW) == "OK"
    assert gate.entries_enabled is True
    db.close()


def test_no_ledger_stays_fail_closed(tmp_path):
    # Unknown broker P&L AND no DB to derive from -> must still fail closed.
    broker = UnknownPnlBroker({"US.AAPL": 100.0})
    gate = EntryGate(enabled=True)
    eng, _ = _engine(tmp_path, broker, _cfg(), gate, db=None)
    assert eng.apply_risk_check(NOW) == "GATE"
    assert gate.entries_enabled is False
