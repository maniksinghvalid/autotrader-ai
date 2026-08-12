"""C2: on live, entries fill async — the stop must attach off the confirmed
fill, not the instant snapshot. Uses the V1 async SimBroker rig."""
from autotrader.breakout_reference import BreakoutReference
from autotrader.config import RiskConfig
from autotrader.main import TradeEngine
from autotrader.sim_broker import SimBroker
from autotrader.strategies.breakout import BreakoutParams, BreakoutStrategy


def _cfg(**kw):
    base = dict(trading_env="PAPER", min_confidence=0.5, max_order_notional=1e6,
                max_position_qty=1000, daily_loss_limit=500, max_gross_exposure=1e6,
                allowed_symbols=frozenset({"US.TEST"}), trailing_stop_pct=5.0)
    base.update(kw)
    return RiskConfig(**base)


def _engine(broker, tick_sleeper):
    strat = BreakoutStrategy(BreakoutParams(symbol="US.TEST", stop_loss_pct=0.05,
                                            take_profit_pct=0.10, confidence=0.9))
    # BreakoutStrategy needs ref_high from a BreakoutReference wired to the SAME
    # broker (matches tests/test_main_loop.py's pattern) — without this, ref_high
    # stays None and evaluate() never emits a BUY (fail-safe: no data => no entry).
    ref = BreakoutReference(broker, lookback=20)
    return TradeEngine(broker, strat, _cfg(), order_qty=10,
                       audit_path="/tmp/at-test-audit.jsonl",
                       hedge_confirm_sleep=tick_sleeper, strategy_ref=ref)


def test_stop_attaches_after_async_entry_fill(tmp_path):
    b = SimBroker({"US.TEST": 100.0}, cash=100000.0, fill_latency_ticks=1,
                  recent_highs={"US.TEST": 90.0})   # price 100 > ref 90 -> breakout BUY
    # The confirm-poll's injected sleep advances the sim market: each backoff
    # tick matures pending fills, exactly like wall time on live.
    eng = _engine(b, tick_sleeper=lambda _s: b.tick_market())
    res = eng.tick()
    assert res.action == "ORDER_PLACED"
    # entry filled during the confirm poll; the stop must now be resting
    stops = [o for o in b.get_open_orders() if o.client_order_id.startswith("at-")]
    assert len(stops) == 1                       # exactly one resting trailing stop
    assert b.get_account().position_qty("US.TEST") == 10


def test_unconfirmed_entry_attaches_no_stop_and_returns_placed(tmp_path):
    b = SimBroker({"US.TEST": 100.0}, cash=100000.0, fill_latency_ticks=99,
                  recent_highs={"US.TEST": 90.0})
    eng = _engine(b, tick_sleeper=lambda _s: None)   # market never advances
    res = eng.tick()
    assert res.action == "ORDER_PLACED"              # entry itself was accepted
    # entry never confirmed -> NO stop submitted (and no mis-directed short)
    working = b.get_open_orders()
    assert len(working) == 1                         # only the unfilled entry rests
