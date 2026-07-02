"""build_engine is the production TradeEngine wiring — the one place that must
pass real sleeps (review Important #2: main() omitted hedge_confirm_sleep, so
the 'bounded live fill-poll' waited ~0ms) and the snapshot-cache size."""
import time

from autotrader.config import RiskConfig
from autotrader.main import build_engine
from autotrader.sim_broker import SimBroker
from autotrader.strategies.threshold import ThresholdStrategy, StrategyParams


def _parts(tmp_path):
    cfg = RiskConfig(trading_env="PAPER", min_confidence=0.6, max_order_notional=2000,
                     max_position_qty=100, daily_loss_limit=500,
                     max_gross_exposure=50000, allowed_symbols=frozenset({"US.AAPL"}))
    strat = ThresholdStrategy(StrategyParams(symbol="US.AAPL", entry_price=100.0,
                                             stop_loss_pct=0.05, take_profit_pct=0.10,
                                             confidence=0.7))
    return SimBroker(quotes={"US.AAPL": 90.0}), strat, cfg


def test_build_engine_wires_real_sleeps(tmp_path):
    b, strat, cfg = _parts(tmp_path)
    eng = build_engine(b, strat, cfg, order_qty=1,
                       audit_path=str(tmp_path / "a.jsonl"))
    assert eng._hedge_confirm_sleep is time.sleep
    assert eng._escalation_sleep is time.sleep


def test_build_engine_snapshot_cache_from_env(tmp_path, monkeypatch):
    b, strat, cfg = _parts(tmp_path)
    monkeypatch.delenv("AUTOTRADER_SNAPSHOT_CACHE_TICKS", raising=False)
    eng = build_engine(b, strat, cfg, order_qty=1,
                       audit_path=str(tmp_path / "a.jsonl"))
    assert eng._snap_cache_ticks == 6
    monkeypatch.setenv("AUTOTRADER_SNAPSHOT_CACHE_TICKS", "12")
    eng = build_engine(b, strat, cfg, order_qty=1,
                       audit_path=str(tmp_path / "a.jsonl"))
    assert eng._snap_cache_ticks == 12
