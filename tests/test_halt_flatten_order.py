"""V4d: on live, flatten SELLs fill async — a cancel_all AFTER _flatten_all
would cancel the liquidation itself. Order must be cancel-then-flatten."""
from datetime import datetime

from autotrader.config import RiskConfig
from autotrader.domain import OrderRequest
from autotrader.lifecycle import EntryGate
from autotrader.main import TradeEngine
from autotrader.sim_broker import SimBroker
from autotrader.strategies.breakout import BreakoutParams, BreakoutStrategy


class _LossBroker(SimBroker):
    def get_account(self):
        snap = super().get_account()
        object.__setattr__(snap, "day_pnl", -5000.0)
        return snap


def test_flatten_sells_survive_the_halt_cancel(tmp_path):
    b = _LossBroker({"US.TEST": 100.0}, cash=100000.0, fill_latency_ticks=5)
    # Seed: one held long (sync entry via a second, synchronous broker view is
    # overkill — just poke the position book the way SimBroker itself does).
    from autotrader.domain import Position
    b._positions["US.TEST"] = Position("US.TEST", 10, 90.0)
    # Seed: one stale working order that the halt SHOULD cancel.
    b._fill_latency = 0   # place the resting order synchronously…
    stale = b.place_order(OrderRequest(symbol="US.TEST", side="SELL", qty=10,
                                       order_type="TRAILING_STOP", limit_price=None,
                                       client_order_id="at-stale-stop",
                                       trail_percent=5.0))
    b._fill_latency = 5   # …then restore async mode for the flatten SELLs
    cfg = RiskConfig(trading_env="PAPER", min_confidence=0.5, max_order_notional=1e6,
                     max_position_qty=1000, daily_loss_limit=500,
                     max_gross_exposure=1e6, allowed_symbols=frozenset({"US.TEST"}),
                     daily_loss_halt=1000.0)
    strat = BreakoutStrategy(BreakoutParams(symbol="US.TEST", stop_loss_pct=0.05,
                                            take_profit_pct=0.10, confidence=0.9))
    eng = TradeEngine(b, strat, cfg, order_qty=1,
                      audit_path=str(tmp_path / "a.jsonl"),
                      entry_gate=EntryGate(enabled=True))
    assert eng.apply_risk_check(datetime(2026, 7, 6, 13, 30)) == "HALT"
    working = b.get_open_orders()
    cids = {o.client_order_id for o in working}
    assert "at-stale-stop" not in cids            # stale order was cancelled
    assert len(working) == 1                       # the flatten SELL still rests
    for _ in range(5):
        b.tick_market()
    assert b.get_account().position_qty("US.TEST") == 0   # liquidation completed
