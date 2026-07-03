"""V5a: RET_OK from a cancel means 'request accepted', not 'cancelled'. The
next escalation stage must not submit while the prior order can still fill."""
from autotrader.config import RiskConfig
from autotrader.domain import OrderRequest
from autotrader.main import TradeEngine
from autotrader.sim_broker import SimBroker
from autotrader.strategies.breakout import BreakoutParams, BreakoutStrategy


def _cfg():
    return RiskConfig(trading_env="PAPER", min_confidence=0.5, max_order_notional=1e6,
                      max_position_qty=1000, daily_loss_limit=500,
                      max_gross_exposure=1e6, allowed_symbols=frozenset({"US.TEST"}),
                      limit_orders_enabled=True, order_cap_bps=1.0,
                      order_cap_ticks=1.0, escalation_dwell_seconds=1.0)


def test_no_next_stage_while_cancel_unconfirmed(tmp_path):
    # Non-marketable limit rests; cancel takes 99 ticks to mature (never, here);
    # sim market never advances -> the cancel is never confirmed.
    b = SimBroker({"US.TEST": 100.0}, cash=100000.0, spread_bps=50.0,
                  cancel_latency_ticks=99)
    strat = BreakoutStrategy(BreakoutParams(symbol="US.TEST", stop_loss_pct=0.05,
                                            take_profit_pct=0.10, confidence=0.9))
    eng = TradeEngine(b, strat, _cfg(), order_qty=10,
                      audit_path=str(tmp_path / "a.jsonl"),
                      escalation_sleep=lambda _s: None)
    req = OrderRequest(symbol="US.TEST", side="BUY", qty=10, order_type="LIMIT",
                       limit_price=99.0, client_order_id="at-esc-test")
    snap = b.get_account()
    ack, term = eng._submit_with_escalation(req, snap, 100.0)
    # cancel never confirmed -> NO re-peg, NO market stage was submitted
    assert term.client_order_id == "at-esc-test"
    working = b.get_open_orders()
    assert {o.client_order_id for o in working} == {"at-esc-test"}


def test_confirmed_cancel_advances_to_repeg(tmp_path):
    # cancel matures after 1 tick; the escalation sleep advances the market.
    b = SimBroker({"US.TEST": 100.0}, cash=100000.0, spread_bps=50.0,
                  cancel_latency_ticks=1)
    strat = BreakoutStrategy(BreakoutParams(symbol="US.TEST", stop_loss_pct=0.05,
                                            take_profit_pct=0.10, confidence=0.9))
    eng = TradeEngine(b, strat, _cfg(), order_qty=10,
                      audit_path=str(tmp_path / "a.jsonl"),
                      escalation_sleep=lambda _s: b.tick_market())
    req = OrderRequest(symbol="US.TEST", side="BUY", qty=10, order_type="LIMIT",
                       limit_price=99.0, client_order_id="at-esc-test2")
    ack, term = eng._submit_with_escalation(req, b.get_account(), 100.0)
    # original cancelled (confirmed), escalation advanced past it
    assert term.client_order_id != "at-esc-test2"
