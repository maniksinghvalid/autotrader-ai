import pytest
from autotrader.config import RiskConfig
from autotrader.sim_broker import SimBroker
from autotrader.strategies.threshold import ThresholdStrategy, StrategyParams
from autotrader.main import TradeEngine
from autotrader.rebalance import RebalanceTrade


def _cfg(**over):
    base = dict(trading_env="PAPER", min_confidence=0.6, max_order_notional=200000,
                max_position_qty=1000, daily_loss_limit=500, max_gross_exposure=1_000_000,
                allowed_symbols=frozenset({"US.AAPL"}))
    base.update(over)
    return RiskConfig(**base)


def _engine(broker, cfg, tmp_path, qty=10):
    strat = ThresholdStrategy(StrategyParams(symbol="US.AAPL", entry_price=100.0,
                                             stop_loss_pct=0.05, take_profit_pct=0.10,
                                             confidence=0.7))
    return TradeEngine(broker=broker, strategy=strat, cfg=cfg,
                       order_qty=qty, audit_path=str(tmp_path / "audit.jsonl"))


def _last_intent(audit_path):
    import json
    with open(audit_path) as f:
        intents = [json.loads(l) for l in f if json.loads(l)["action"] == "intent"]
    return intents[-1]


def test_flag_off_signal_is_market_byte_for_byte(tmp_path):
    b = SimBroker(quotes={"US.AAPL": 101.0}, cash=1_000_000.0)
    eng = _engine(b, _cfg(limit_orders_enabled=False), tmp_path)
    assert eng.tick().action == "ORDER_PLACED"
    intent = _last_intent(str(tmp_path / "audit.jsonl"))
    assert intent["order_type"] == "MARKET"
    assert intent["limit_price"] is None            # exactly today's request shape


def test_flag_on_signal_emits_capped_buy_limit(tmp_path):
    b = SimBroker(quotes={"US.AAPL": 101.0}, cash=1_000_000.0, spread_bps=10.0)
    eng = _engine(b, _cfg(limit_orders_enabled=True, order_cap_bps=20.0), tmp_path)
    assert eng.tick().action == "ORDER_PLACED"      # marketable: 20bps cap > 10bps spread
    intent = _last_intent(str(tmp_path / "audit.jsonl"))
    assert intent["order_type"] == "LIMIT"
    # 101 + 20bps = 101.202, rounded to the nearest cent tick (capped_limit_price
    # rounds to tick_size(ref) per Task 3 / test_limit_pricing.py) -> 101.20.
    assert intent["limit_price"] == pytest.approx(round((101.0 + 101.0 * 0.0020) / 0.01) * 0.01)


def test_flag_off_rebalance_sell_is_market(tmp_path):
    b = SimBroker(quotes={"US.AAPL": 100.0}, cash=1_000_000.0)
    b._positions["US.AAPL"] = __import__("autotrader.domain", fromlist=["Position"]).Position("US.AAPL", 50, 100.0)
    eng = _engine(b, _cfg(limit_orders_enabled=False), tmp_path)
    trade = RebalanceTrade("US.AAPL", "SELL", 10, "TRIM", 40)
    assert eng.submit_rebalance_order(trade, 100.0, "rbal-x").action == "ORDER_PLACED"
    assert _last_intent(str(tmp_path / "audit.jsonl"))["order_type"] == "MARKET"


def test_flag_on_rebalance_sell_emits_capped_sell_limit(tmp_path):
    from autotrader.domain import Position
    b = SimBroker(quotes={"US.AAPL": 100.0}, cash=1_000_000.0, spread_bps=5.0)
    b._positions["US.AAPL"] = Position("US.AAPL", 50, 100.0)
    eng = _engine(b, _cfg(limit_orders_enabled=True, order_cap_bps=10.0), tmp_path)
    trade = RebalanceTrade("US.AAPL", "SELL", 10, "TRIM", 40)
    assert eng.submit_rebalance_order(trade, 100.0, "rbal-x").action == "ORDER_PLACED"
    intent = _last_intent(str(tmp_path / "audit.jsonl"))
    assert intent["order_type"] == "LIMIT"
    assert intent["limit_price"] == pytest.approx(100.0 - 100.0 * 0.0010)  # 100 - 10bps
