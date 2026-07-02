"""Refresh-token budgeting: NO_SIGNAL ticks reuse a cached account snapshot;
any routed signal always re-fetches fresh (cached data never reaches the risk
core). Default snapshot_cache_ticks=1 preserves today's fetch-per-tick."""
from autotrader.config import RiskConfig
from autotrader.main import TradeEngine
from autotrader.sim_broker import SimBroker
from autotrader.strategies.threshold import ThresholdStrategy, StrategyParams


class _CountingBroker(SimBroker):
    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self.account_calls = 0

    def get_account(self):
        self.account_calls += 1
        return super().get_account()


def _cfg():
    return RiskConfig(trading_env="PAPER", min_confidence=0.6,
                      max_order_notional=20000, max_position_qty=100,
                      daily_loss_limit=500, max_gross_exposure=100000,
                      allowed_symbols=frozenset({"US.AAPL"}))


def _engine(broker, cache_ticks, entry_price=200.0):
    strat = ThresholdStrategy(StrategyParams(symbol="US.AAPL", entry_price=entry_price,
                                             stop_loss_pct=0.05, take_profit_pct=0.10,
                                             confidence=0.7))
    return TradeEngine(broker=broker, strategy=strat, cfg=_cfg(), order_qty=1,
                       audit_path="/dev/null", snapshot_cache_ticks=cache_ticks)


def test_no_signal_ticks_share_one_snapshot(tmp_path):
    b = _CountingBroker(quotes={"US.AAPL": 100.0}, cash=10000.0)  # 100 < entry 200 -> NO_SIGNAL
    eng = _engine(b, cache_ticks=3)
    for _ in range(3):
        assert eng.tick().action == "NO_SIGNAL"
    assert b.account_calls == 1
    for _ in range(3):
        eng.tick()
    assert b.account_calls == 2


def test_default_is_fetch_per_tick(tmp_path):
    b = _CountingBroker(quotes={"US.AAPL": 100.0}, cash=10000.0)
    eng = _engine(b, cache_ticks=1)
    eng.tick(); eng.tick()
    assert b.account_calls == 2


def test_signal_routes_on_fresh_snapshot(tmp_path):
    b = _CountingBroker(quotes={"US.AAPL": 100.0}, cash=10000.0)
    eng = _engine(b, cache_ticks=5, entry_price=90.0)   # 100 >= 90 -> BUY
    assert eng.tick().action == "ORDER_PLACED"
    # one cached-tick fetch + one fresh pre-routing fetch (+ the stop-attach
    # refetch inside _attach_trailing_stop only when trailing_stop_pct > 0)
    assert b.account_calls == 2
