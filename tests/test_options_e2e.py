from datetime import date, timedelta

from autotrader.config import RiskConfig
from autotrader.domain import OverlayType, Signal
from autotrader.main import TradeEngine
from autotrader.options.chain import OptionQuote
from autotrader.sim_broker import SimBroker
from autotrader.strategies.threshold import ThresholdStrategy, StrategyParams


ASOF = date(2026, 6, 16)


def _chains():
    return {
        ("US.AAPL", "CALL"): [
            OptionQuote("US.AAPL260721C210000", "US.AAPL", ASOF + timedelta(days=35),
                        210, "CALL", 0.30, 1.5)],
        ("US.AAPL", "PUT"): [
            OptionQuote("US.AAPL260721P190000", "US.AAPL", ASOF + timedelta(days=35),
                        190, "PUT", -0.29, 1.4)],
    }


def _cfg(**over):
    base = dict(trading_env="PAPER", min_confidence=0.6, max_order_notional=2000,
                max_position_qty=100, daily_loss_limit=500, max_gross_exposure=50000,
                allowed_symbols=frozenset({"US.AAPL"}), daily_loss_halt=1000,
                allowed_overlays=frozenset({"COVERED_CALL", "PROTECTIVE_PUT"}),
                max_option_contracts=5, max_option_premium_per_trade=800.0)
    base.update(over)
    return RiskConfig(**base)


def _engine(broker, cfg, tmp_path):
    # The overlay tests drive submit_external_signal, not tick(), so the strategy
    # is just a required constructor arg; any valid StrategyParams works.
    strat = ThresholdStrategy(StrategyParams(symbol="US.AAPL", entry_price=1.0,
                                             stop_loss_pct=0.05, take_profit_pct=0.10,
                                             confidence=0.7))
    return TradeEngine(broker, strat, cfg, order_qty=10,
                       audit_path=str(tmp_path / "audit.jsonl"),
                       today_fn=lambda: ASOF)


def _broker(shares=100):
    b = SimBroker(quotes={"US.AAPL": 200.0, "US.AAPL260721C210000": 1.5,
                          "US.AAPL260721P190000": 1.4},
                  option_chains=_chains())
    if shares:
        from autotrader.domain import OrderRequest
        b.place_order(OrderRequest(symbol="US.AAPL", side="BUY", qty=shares,
                                   order_type="MARKET", limit_price=None,
                                   client_order_id="seed"))
    return b


def test_covered_call_places_short_call(tmp_path):
    b = _broker(shares=200)
    eng = _engine(b, _cfg(), tmp_path)
    res = eng.submit_external_signal(
        Signal("US.AAPL", "SELL", 0.7, "Covered Call", overlay=OverlayType.COVERED_CALL))
    assert res.action == "OVERLAY_PLACED", res
    acc = b.get_account()
    held = {p.symbol: p.qty for p in acc.positions}
    assert held["US.AAPL260721C210000"] == -2   # short 2 calls vs 200 shares


def test_protective_put_places_long_put(tmp_path):
    b = _broker(shares=100)
    eng = _engine(b, _cfg(), tmp_path)
    res = eng.submit_external_signal(
        Signal("US.AAPL", "SELL", 0.7, "Protective Put", overlay=OverlayType.PROTECTIVE_PUT))
    assert res.action == "OVERLAY_PLACED", res
    held = {p.symbol: p.qty for p in b.get_account().positions}
    assert held["US.AAPL260721P190000"] == 1


def test_overlay_skipped_no_underlying(tmp_path):
    b = _broker(shares=0)
    eng = _engine(b, _cfg(), tmp_path)
    res = eng.submit_external_signal(
        Signal("US.AAPL", "SELL", 0.7, "Covered Call", overlay=OverlayType.COVERED_CALL))
    assert res.action == "SKIP_NO_UNDERLYING", res


def test_overlay_low_confidence_dropped(tmp_path):
    b = _broker(shares=200)
    eng = _engine(b, _cfg(), tmp_path)
    res = eng.submit_external_signal(
        Signal("US.AAPL", "SELL", 0.3, "Covered Call", overlay=OverlayType.COVERED_CALL))
    assert res.action == "DROPPED_LOW_CONFIDENCE", res


def test_equity_signal_unaffected(tmp_path):
    b = _broker(shares=0)
    eng = _engine(b, _cfg(), tmp_path)
    res = eng.submit_external_signal(Signal("US.AAPL", "BUY", 0.7, "plain"))
    assert res.action == "ORDER_PLACED", res
