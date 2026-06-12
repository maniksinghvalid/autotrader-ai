import json
import pytest
from autotrader.domain import Position
from autotrader.config import RiskConfig
from autotrader.sim_broker import SimBroker
from autotrader.strategies.threshold import ThresholdStrategy, StrategyParams
from autotrader.main import TradeEngine


def _cfg(**over):
    base = dict(trading_env="PAPER", min_confidence=0.6, max_order_notional=20000,
                max_position_qty=100, daily_loss_limit=500, max_gross_exposure=100000,
                allowed_symbols=frozenset({"US.AAPL"}))
    base.update(over)
    return RiskConfig(**base)


def _engine(broker, cfg=None, qty=10, audit_path="x.jsonl", tmp_path=None):
    path = str(tmp_path / "audit.jsonl") if tmp_path else audit_path
    strat = ThresholdStrategy(StrategyParams(symbol="US.AAPL", entry_price=100.0,
                                             stop_loss_pct=0.05, take_profit_pct=0.10,
                                             confidence=0.7))
    return TradeEngine(broker=broker, strategy=strat, cfg=cfg or _cfg(),
                       order_qty=qty, audit_path=path)


def test_tick_places_buy_when_threshold_crosses(tmp_path):
    b = SimBroker(quotes={"US.AAPL": 101.0}, cash=100000.0)
    eng = _engine(b, tmp_path=tmp_path)
    result = eng.tick()
    assert result.action == "ORDER_PLACED"
    assert b.get_account().position_qty("US.AAPL") == 10


def test_tick_drops_low_confidence_signal(tmp_path):
    b = SimBroker(quotes={"US.AAPL": 101.0}, cash=100000.0)
    eng = _engine(b, cfg=_cfg(min_confidence=0.9), tmp_path=tmp_path)
    result = eng.tick()
    assert result.action == "DROPPED_LOW_CONFIDENCE"
    assert b.get_account().position_qty("US.AAPL") == 0


def test_tick_respects_risk_core_rejection(tmp_path):
    b = SimBroker(quotes={"US.AAPL": 101.0}, cash=100000.0)
    eng = _engine(b, cfg=_cfg(allowed_symbols=frozenset()), tmp_path=tmp_path)
    result = eng.tick()
    assert result.action == "REJECTED_BY_RISK"
    assert b.get_account().position_qty("US.AAPL") == 0


def test_tick_no_signal_when_below_entry(tmp_path):
    b = SimBroker(quotes={"US.AAPL": 99.0}, cash=100000.0)
    eng = _engine(b, tmp_path=tmp_path)
    assert eng.tick().action == "NO_SIGNAL"


def test_shutdown_cancels_all_open_orders(tmp_path):
    b = SimBroker(quotes={"US.AAPL": 101.0}, cash=100000.0, auto_fill=False)
    eng = _engine(b, tmp_path=tmp_path)
    eng.tick()
    assert len(b.get_open_orders()) == 1
    eng.shutdown()
    assert b.get_open_orders() == []
