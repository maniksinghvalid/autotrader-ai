"""Pre-market external BUY signals are DEFERRED to the entry window, not dropped.

External signals arrive once (the inbox consumes each drop exactly once), so a BUY
that lands before ENTRY_OPEN must be held and replayed when entries open — otherwise
a genuine HOLD->BUY transition is lost purely because of when the routine fired.
Internal strategy ticks are NOT deferred: they re-emit every loop, so they self-retry.
"""
from autotrader.config import RiskConfig
from autotrader.domain import Signal
from autotrader.lifecycle import EntryGate
from autotrader.sim_broker import SimBroker
from autotrader.strategies.threshold import ThresholdStrategy, StrategyParams
from autotrader.main import TradeEngine


def _cfg(**over):
    base = dict(trading_env="PAPER", min_confidence=0.6, max_order_notional=20000,
                max_position_qty=100, daily_loss_limit=500, max_gross_exposure=100000,
                allowed_symbols=frozenset({"US.AAPL"}))
    base.update(over)
    return RiskConfig(**base)


def _engine(broker, gate, cfg=None, qty=10, tmp_path=None):
    strat = ThresholdStrategy(StrategyParams(symbol="US.AAPL", entry_price=100.0,
                                             stop_loss_pct=0.05, take_profit_pct=0.10,
                                             confidence=0.7))
    return TradeEngine(broker=broker, strategy=strat, cfg=cfg or _cfg(),
                       order_qty=qty, audit_path=str(tmp_path / "audit.jsonl"),
                       entry_gate=gate)


def _buy(sym="US.AAPL", conf=0.9):
    return Signal(symbol=sym, direction="BUY", confidence=conf, rationale="ext")


def test_external_buy_is_deferred_not_dropped_when_entries_closed(tmp_path):
    b = SimBroker(quotes={"US.AAPL": 101.0}, cash=100000.0)
    eng = _engine(b, EntryGate(enabled=False), tmp_path=tmp_path)
    res = eng.submit_external_signal(_buy())
    assert res.action == "ENTRY_DEFERRED"
    assert b.get_account().position_qty("US.AAPL") == 0     # not placed yet
    assert len(eng._deferred_entries) == 1                  # held for the window


def test_deferred_buy_is_placed_when_entries_open_and_flushed(tmp_path):
    b = SimBroker(quotes={"US.AAPL": 101.0}, cash=100000.0)
    gate = EntryGate(enabled=False)
    eng = _engine(b, gate, tmp_path=tmp_path)
    eng.submit_external_signal(_buy())            # deferred pre-market

    gate.open()                                    # ENTRY_OPEN fires
    results = eng.flush_deferred_entries()

    assert [r.action for r in results] == ["ORDER_PLACED"]
    assert b.get_account().position_qty("US.AAPL") == 10
    assert eng._deferred_entries == []             # queue drained


def test_flush_is_a_noop_while_entries_still_closed(tmp_path):
    b = SimBroker(quotes={"US.AAPL": 101.0}, cash=100000.0)
    eng = _engine(b, EntryGate(enabled=False), tmp_path=tmp_path)
    eng.submit_external_signal(_buy())

    results = eng.flush_deferred_entries()         # gate still closed

    assert results == []
    assert b.get_account().position_qty("US.AAPL") == 0
    assert len(eng._deferred_entries) == 1         # still held, not lost


def test_low_confidence_external_signal_is_dropped_not_deferred(tmp_path):
    # The confidence filter precedes the entry gate, so a sub-threshold signal must
    # still drop outright — deferral must not resurrect it at the open.
    b = SimBroker(quotes={"US.AAPL": 101.0}, cash=100000.0)
    eng = _engine(b, EntryGate(enabled=False), cfg=_cfg(min_confidence=0.9),
                  tmp_path=tmp_path)
    res = eng.submit_external_signal(_buy(conf=0.5))
    assert res.action == "DROPPED_LOW_CONFIDENCE"
    assert eng._deferred_entries == []


def test_redeferring_same_symbol_keeps_only_the_latest(tmp_path):
    # A newer BUY for the same symbol supersedes an older deferred one, so the queue
    # cannot grow unbounded and a stale duplicate never fires alongside the fresh one.
    b = SimBroker(quotes={"US.AAPL": 101.0}, cash=100000.0)
    eng = _engine(b, EntryGate(enabled=False), tmp_path=tmp_path)
    eng.submit_external_signal(_buy(conf=0.7))
    eng.submit_external_signal(_buy(conf=0.95))
    assert len(eng._deferred_entries) == 1
    assert eng._deferred_entries[0].confidence == 0.95
