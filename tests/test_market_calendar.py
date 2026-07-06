"""Trading calendar (spec W3): weekends and configured holidays run nothing —
no lifecycle jobs, no tick, no inbox (review Important #4: Saturday BUYs off
Friday's close). Early-close support is a tracked PRE-LIVE follow-up."""
from datetime import date, datetime
from zoneinfo import ZoneInfo

from autotrader.clock import is_trading_day

_NY = ZoneInfo("America/New_York")


def test_weekday_is_trading_day():
    assert is_trading_day(date(2026, 7, 6)) is True          # Monday


def test_weekend_is_not():
    assert is_trading_day(date(2026, 7, 4)) is False         # Saturday
    assert is_trading_day(date(2026, 7, 5)) is False         # Sunday


def test_configured_holiday_is_not():
    hol = frozenset({date(2026, 7, 3)})
    assert is_trading_day(date(2026, 7, 3), hol) is False    # July 4th observed
    assert is_trading_day(date(2026, 7, 2), hol) is True


def test_runner_skips_everything_on_non_trading_day(tmp_path):
    from autotrader.clock import FixedClock
    from autotrader.config import RiskConfig
    from autotrader.db import DB
    from autotrader.lifecycle import EntryGate
    from autotrader.main import TradeEngine
    from autotrader.runner import SessionRunner
    from autotrader.scheduler import LifecycleScheduler
    from autotrader.sim_broker import SimBroker
    from autotrader.strategies.threshold import ThresholdStrategy, StrategyParams
    from autotrader.watchdog import Watchdog

    b = SimBroker(quotes={"US.AAPL": 101.0}, cash=100000.0)
    db = DB(str(tmp_path / "cal.db"))
    gate = EntryGate(enabled=False)
    cfg = RiskConfig(trading_env="PAPER", min_confidence=0.6, max_order_notional=20000,
                     max_position_qty=100, daily_loss_limit=500,
                     max_gross_exposure=100000, allowed_symbols=frozenset({"US.AAPL"}))
    strat = ThresholdStrategy(StrategyParams(symbol="US.AAPL", entry_price=100.0,
                                             stop_loss_pct=0.05, take_profit_pct=0.10,
                                             confidence=0.7))
    eng = TradeEngine(broker=b, strategy=strat, cfg=cfg, order_qty=1,
                      audit_path=str(tmp_path / "audit.jsonl"), db=db, entry_gate=gate)
    runner = SessionRunner(engine=eng, broker=b, db=db, gate=gate,
                           scheduler=LifecycleScheduler(),
                           watchdog=Watchdog(health_check=lambda: True,
                                             reconcile=lambda: None, sleep=lambda s: None),
                           clock=FixedClock(datetime(2026, 7, 4, 9, 46, tzinfo=_NY)),
                           sleep=lambda s: None,
                           trading_day_fn=is_trading_day)
    # Saturday 09:46: ENTRY_OPEN would fire and the 101>=100 BUY would place.
    assert runner.run_once(datetime(2026, 7, 4, 9, 46, tzinfo=_NY)) == "NON_TRADING_DAY"
    assert gate.entries_enabled is False
    assert b.get_account().position_qty("US.AAPL") == 0
    db.close()
