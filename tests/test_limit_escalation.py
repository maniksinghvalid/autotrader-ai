import pytest
from autotrader.config import RiskConfig
from autotrader.domain import OrderRequest, OrderState, Position
from autotrader.sim_broker import SimBroker
from autotrader.strategies.threshold import ThresholdStrategy, StrategyParams
from autotrader.main import TradeEngine


def _cfg(**over):
    base = dict(trading_env="PAPER", min_confidence=0.6, max_order_notional=1_000_000,
                max_position_qty=10_000, daily_loss_limit=500, max_gross_exposure=100_000_000,
                allowed_symbols=frozenset({"US.AAPL"}),
                limit_orders_enabled=True, order_cap_bps=5.0)
    base.update(over)
    return RiskConfig(**base)


def _engine(broker, cfg, tmp_path):
    strat = ThresholdStrategy(StrategyParams(symbol="US.AAPL", entry_price=100.0,
                                             stop_loss_pct=0.05, take_profit_pct=0.10,
                                             confidence=0.7))
    return TradeEngine(broker=broker, strategy=strat, cfg=cfg,
                       order_qty=10, audit_path=str(tmp_path / "audit.jsonl"))


def test_marketable_limit_fills_without_escalation(tmp_path):
    # 50 bps cap, 10 bps spread -> marketable -> fills on the first submit, no market fallback.
    b = SimBroker(quotes={"US.AAPL": 100.0}, cash=1_000_000.0, spread_bps=10.0)
    eng = _engine(b, _cfg(order_cap_bps=50.0), tmp_path)
    assert eng.tick().action == "ORDER_PLACED"
    assert b.get_account().position_qty("US.AAPL") == 10
    assert b.get_open_orders() == []                 # nothing left resting


def test_unfilled_limit_escalates_to_market_and_completes(tmp_path):
    # 5 bps cap but 100 bps spread -> limit NOT marketable, rests; re-peg still not
    # marketable; MARKET fallback completes the fill (a risk exit must complete).
    b = SimBroker(quotes={"US.AAPL": 100.0}, cash=1_000_000.0, spread_bps=100.0)
    eng = _engine(b, _cfg(order_cap_bps=5.0), tmp_path)
    assert eng.tick().action == "ORDER_PLACED"
    assert b.get_account().position_qty("US.AAPL") == 10   # filled via MARKET fallback
    # MARKET buy paid the ask (100 * (1 + 0.01)) = 101.0.
    assert b.reconcile_fills(None)[-1].price == pytest.approx(101.0)
    assert b.get_open_orders() == []                       # resting limits cancelled


def test_escalation_records_terminal_state_only(tmp_path):
    import json
    b = SimBroker(quotes={"US.AAPL": 100.0}, cash=1_000_000.0, spread_bps=100.0)
    eng = _engine(b, _cfg(order_cap_bps=5.0), tmp_path)
    eng.tick()
    with open(tmp_path / "audit.jsonl") as f:
        acks = [json.loads(l) for l in f if json.loads(l)["action"] == "ack"]
    # last ack is the filled MARKET fallback.
    assert acks[-1]["state"] == OrderState.FILLED.value
