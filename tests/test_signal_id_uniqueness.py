"""signal_id must be unique across process runs.

The engine numbers signals sig-1, sig-2, ... from a per-instance counter that
resets to 1 every process start. `signals.signal_id` is UNIQUE and record_signal
does INSERT OR IGNORE, so after a restart the fresh sig-1.. collide with the first
run's rows and are silently dropped from the projection (and, via
make_client_order_id, cids can collide too). A per-session prefix keeps ids unique
across restarts.
"""
from autotrader.config import RiskConfig
from autotrader.db import DB
from autotrader.domain import Signal
from autotrader.sim_broker import SimBroker
from autotrader.strategies.threshold import ThresholdStrategy, StrategyParams
from autotrader.main import TradeEngine


def _cfg(**over):
    base = dict(trading_env="PAPER", min_confidence=0.6, max_order_notional=20000,
                max_position_qty=100, daily_loss_limit=500, max_gross_exposure=100000,
                allowed_symbols=frozenset({"US.AAPL"}))
    base.update(over)
    return RiskConfig(**base)


def _engine(broker, db, tmp_path, session_id=None):
    strat = ThresholdStrategy(StrategyParams(symbol="US.AAPL", entry_price=100.0,
                                             stop_loss_pct=0.05, take_profit_pct=0.10,
                                             confidence=0.7))
    return TradeEngine(broker=broker, strategy=strat, cfg=_cfg(), order_qty=10,
                       audit_path=str(tmp_path / "audit.jsonl"), db=db,
                       session_id=session_id)


def _buy():
    return Signal(symbol="US.AAPL", direction="BUY", confidence=0.9, rationale="ext")


def test_signals_from_separate_sessions_are_both_recorded(tmp_path):
    # Two engines over one DB simulate a process restart: the second run's counter
    # also starts at 1, but its signal must NOT be swallowed by the first run's row.
    db = DB(str(tmp_path / "s.db"))
    b = SimBroker(quotes={"US.AAPL": 101.0}, cash=100000.0)

    _engine(b, db, tmp_path).submit_external_signal(_buy())   # session 1 -> first signal
    _engine(b, db, tmp_path).submit_external_signal(_buy())   # session 2 (restart) -> first signal

    n = db._conn.execute("SELECT COUNT(*) FROM signals").fetchone()[0]
    assert n == 2, "both sessions' signals must persist (no cross-restart id collision)"
    db.close()


def test_default_sessions_get_distinct_signal_id_prefixes(tmp_path):
    db = DB(str(tmp_path / "s.db"))
    b = SimBroker(quotes={"US.AAPL": 101.0}, cash=100000.0)
    _engine(b, db, tmp_path).submit_external_signal(_buy())
    _engine(b, db, tmp_path).submit_external_signal(_buy())
    ids = [r[0] for r in db._conn.execute("SELECT signal_id FROM signals").fetchall()]
    assert len(ids) == 2 and ids[0] != ids[1]
    db.close()


def test_injected_session_id_appears_in_signal_id(tmp_path):
    db = DB(str(tmp_path / "s.db"))
    b = SimBroker(quotes={"US.AAPL": 101.0}, cash=100000.0)
    _engine(b, db, tmp_path, session_id="sess9").submit_external_signal(_buy())
    sid = db._conn.execute("SELECT signal_id FROM signals").fetchone()[0]
    assert sid.startswith("sig-sess9-"), sid
    db.close()
