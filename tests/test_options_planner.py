from datetime import date, timedelta

from autotrader.options.chain import OptionQuote
from autotrader.sim_broker import SimBroker


def _asof():
    return date(2026, 6, 16)


def _call_chain():
    a = _asof()
    return [
        OptionQuote("US.AAPL260721C210000", "US.AAPL", a + timedelta(days=35),
                    210, "CALL", 0.30, 1.5),
        OptionQuote("US.AAPL260721C220000", "US.AAPL", a + timedelta(days=35),
                    220, "CALL", 0.12, 0.6),
    ]


def test_sim_broker_returns_seeded_chain():
    b = SimBroker(quotes={"US.AAPL": 200.0},
                  option_chains={("US.AAPL", "CALL"): _call_chain()})
    rows = b.get_option_chain("US.AAPL", "CALL")
    assert [r.code for r in rows] == [q.code for q in _call_chain()]


def test_sim_broker_unknown_chain_is_empty():
    b = SimBroker(quotes={"US.AAPL": 200.0})
    assert b.get_option_chain("US.AAPL", "PUT") == []


def test_sim_broker_chain_lookup_is_case_insensitive_on_right():
    b = SimBroker(quotes={"US.AAPL": 200.0},
                  option_chains={("US.AAPL", "CALL"): _call_chain()})
    # lowercase right still resolves (no silent empty)
    assert [r.code for r in b.get_option_chain("us.aapl", "call")] == \
        [q.code for q in _call_chain()]


from autotrader.config import RiskConfig
from autotrader.domain import AccountSnapshot, OverlayType, Position, Signal
from autotrader.options.planner import (
    build_overlay_plan, OverlayPlan, OverlaySkip,
)


def _put_chain():
    a = _asof()
    return [OptionQuote("US.AAPL260721P190000", "US.AAPL", a + timedelta(days=35),
                        190, "PUT", -0.29, 1.4)]


def _cfg(**over):
    base = dict(trading_env="PAPER", min_confidence=0.6, max_order_notional=2000,
                max_position_qty=100, daily_loss_limit=500, max_gross_exposure=50000,
                allowed_symbols=frozenset({"US.AAPL"}), daily_loss_halt=1000,
                allowed_overlays=frozenset({"COVERED_CALL", "PROTECTIVE_PUT"}),
                max_option_contracts=5, max_option_premium_per_trade=800.0)
    base.update(over)
    return RiskConfig(**base)


def _snap(shares=100):
    pos = (Position("US.AAPL", shares, 200.0),) if shares else ()
    return AccountSnapshot(cash=50000, total_assets=70000, day_pnl=0.0,
                           stale=False, positions=pos)


def _sig(overlay):
    return Signal(symbol="US.AAPL", direction="SELL", confidence=0.7,
                  rationale="x", overlay=overlay)


def _broker():
    return SimBroker(quotes={"US.AAPL": 200.0}, option_chains={
        ("US.AAPL", "CALL"): _call_chain(),
        ("US.AAPL", "PUT"): _put_chain(),
    })


def test_covered_call_plan_builds_short_call_leg():
    plan = build_overlay_plan(_sig(OverlayType.COVERED_CALL), _snap(200),
                              _broker(), _cfg(), "sig-1", _asof())
    assert isinstance(plan, OverlayPlan)
    assert len(plan.legs) == 1
    leg = plan.legs[0].request
    assert leg.side == "SELL" and leg.option.right == "CALL"
    assert leg.qty == 2                       # 200 shares -> 2 contracts
    assert leg.option.code == "US.AAPL260721C210000"   # closest 0.30 delta
    assert plan.exit.dte_to_close == 7


def test_protective_put_plan_builds_long_put_leg():
    plan = build_overlay_plan(_sig(OverlayType.PROTECTIVE_PUT), _snap(100),
                              _broker(), _cfg(), "sig-2", _asof())
    assert isinstance(plan, OverlayPlan)
    assert plan.legs[0].request.side == "BUY"
    assert plan.legs[0].request.option.right == "PUT"
    assert plan.legs[0].request.qty == 1


def test_skip_no_underlying():
    skip = build_overlay_plan(_sig(OverlayType.COVERED_CALL), _snap(0),
                              _broker(), _cfg(), "s", _asof())
    assert isinstance(skip, OverlaySkip) and skip.reason == "SKIP_NO_UNDERLYING"


def test_skip_overlay_disabled():
    skip = build_overlay_plan(_sig(OverlayType.COVERED_CALL), _snap(100),
                              _broker(), _cfg(allowed_overlays=frozenset()),
                              "s", _asof())
    assert isinstance(skip, OverlaySkip) and skip.reason == "SKIP_OVERLAY_DISABLED"


def test_skip_unsupported_overlay(monkeypatch):
    from autotrader.options import overlays
    monkeypatch.delitem(overlays.REGISTRY, OverlayType.COLLAR)
    skip = build_overlay_plan(_sig(OverlayType.COLLAR), _snap(100),
                              _broker(), _cfg(allowed_overlays=frozenset({"COLLAR"})),
                              "s", _asof())
    assert isinstance(skip, OverlaySkip) and skip.reason == "SKIP_UNSUPPORTED_OVERLAY"


def test_skip_no_contract_in_window():
    skip = build_overlay_plan(_sig(OverlayType.COVERED_CALL), _snap(100),
                              _broker(), _cfg(option_dte_min=60, option_dte_max=90),
                              "s", _asof())
    assert isinstance(skip, OverlaySkip) and skip.reason == "SKIP_NO_CONTRACT"


def test_contracts_capped_by_config():
    plan = build_overlay_plan(_sig(OverlayType.COVERED_CALL), _snap(1000),
                              _broker(), _cfg(max_option_contracts=3), "s", _asof())
    assert isinstance(plan, OverlayPlan) and plan.legs[0].request.qty == 3


def test_build_overlay_plan_requires_overlay():
    import pytest
    sig = Signal(symbol="US.AAPL", direction="BUY", confidence=0.7, rationale="x")  # no overlay
    with pytest.raises(ValueError):
        build_overlay_plan(sig, _snap(100), _broker(), _cfg(), "s", _asof())


def _far():
    return _asof() + timedelta(days=300)


def _rich_chains():
    a, near, far = _asof(), _asof() + timedelta(days=35), _far()
    return {
        ("US.AAPL", "CALL"): [
            OptionQuote("US.AAPL270412C180000", "US.AAPL", far, 180, "CALL", 0.80, 30.0),
            OptionQuote("US.AAPL270412C195000", "US.AAPL", far, 195, "CALL", 0.70, 20.0),
            OptionQuote("US.AAPL260721C210000", "US.AAPL", near, 210, "CALL", 0.30, 1.5),
        ],
        ("US.AAPL", "PUT"): [
            OptionQuote("US.AAPL260721P200000", "US.AAPL", near, 200, "PUT", -0.45, 4.0),
            OptionQuote("US.AAPL260721P190000", "US.AAPL", near, 190, "PUT", -0.30, 2.0),
            OptionQuote("US.AAPL260721P185000", "US.AAPL", near, 185, "PUT", -0.22, 1.5),
        ],
    }


def _rich_broker():
    return SimBroker(quotes={"US.AAPL": 200.0}, option_chains=_rich_chains())


def _cfgN(**over):
    # all four phantom overlays enabled; premium cap high enough for LEAP/PMCC long legs
    base = dict(allowed_overlays=frozenset({
        "COVERED_CALL", "PROTECTIVE_PUT", "COLLAR",
        "BEAR_PUT_SPREAD", "CALL_DIAGONAL", "LEAP"}),
        max_option_premium_per_trade=5000.0, option_default_contracts=1)
    base.update(over)
    return _cfg(**base)


def test_collar_plan_is_long_put_and_short_call_same_expiry():
    plan = build_overlay_plan(_sig(OverlayType.COLLAR), _snap(100),
                              _rich_broker(), _cfgN(), "s", _asof())
    assert isinstance(plan, OverlayPlan)
    by_side = {l.request.side: l for l in plan.legs}
    assert by_side["BUY"].request.option.right == "PUT"
    assert by_side["SELL"].request.option.right == "CALL"
    assert by_side["BUY"].request.option.expiry == by_side["SELL"].request.option.expiry
    assert by_side["BUY"].request.option.code == "US.AAPL260721P190000"  # 0.30 put
    assert by_side["SELL"].request.option.code == "US.AAPL260721C210000"  # 0.30 call


def test_bear_put_spread_plan_long_higher_short_lower_same_expiry():
    plan = build_overlay_plan(_sig(OverlayType.BEAR_PUT_SPREAD), _snap(0),
                              _rich_broker(), _cfgN(), "s", _asof())
    assert isinstance(plan, OverlayPlan)
    by_side = {l.request.side: l for l in plan.legs}
    assert by_side["BUY"].request.option.strike == 200   # 0.45 delta long
    assert by_side["SELL"].request.option.strike == 185  # 0.22 (closest to 0.25) short
    assert by_side["BUY"].request.qty == 1               # option_default_contracts
    assert by_side["BUY"].request.option.expiry == by_side["SELL"].request.option.expiry


def test_call_diagonal_plan_long_far_short_near_different_expiries():
    plan = build_overlay_plan(_sig(OverlayType.CALL_DIAGONAL), _snap(0),
                              _rich_broker(), _cfgN(), "s", _asof())
    assert isinstance(plan, OverlayPlan)
    by_side = {l.request.side: l for l in plan.legs}
    assert by_side["BUY"].request.option.strike == 180          # deep-ITM 0.80
    assert by_side["BUY"].request.option.expiry == _far()
    assert by_side["SELL"].request.option.strike == 210         # near 0.30
    assert by_side["BUY"].request.option.expiry > by_side["SELL"].request.option.expiry


def test_leap_plan_single_long_call_sized_by_default_contracts():
    plan = build_overlay_plan(_sig(OverlayType.LEAP), _snap(0),
                              _rich_broker(), _cfgN(option_default_contracts=2), "s", _asof())
    assert isinstance(plan, OverlayPlan)
    assert len(plan.legs) == 1
    leg = plan.legs[0].request
    assert leg.side == "BUY" and leg.option.right == "CALL"
    assert leg.option.code == "US.AAPL270412C195000" and leg.qty == 2


def test_non_covered_strategy_sized_zero_is_disabled():
    skip = build_overlay_plan(_sig(OverlayType.LEAP), _snap(0), _rich_broker(),
                              _cfgN(option_default_contracts=0), "s", _asof())
    assert isinstance(skip, OverlaySkip) and skip.reason == "SKIP_OVERLAY_DISABLED"


def test_bear_put_spread_invalid_structure_when_no_debit():
    # A chain where the two selected puts invert the debit (long premium < short).
    a, near = _asof(), _asof() + timedelta(days=35)
    bad = {("US.AAPL", "PUT"): [
        OptionQuote("LONG", "US.AAPL", near, 200, "PUT", -0.45, 1.0),   # long, cheap
        OptionQuote("SHORT", "US.AAPL", near, 185, "PUT", -0.25, 4.0),  # short, rich
    ]}
    b = SimBroker(quotes={"US.AAPL": 200.0}, option_chains=bad)
    skip = build_overlay_plan(_sig(OverlayType.BEAR_PUT_SPREAD), _snap(0), b,
                              _cfgN(), "s", _asof())
    assert isinstance(skip, OverlaySkip) and skip.reason == "SKIP_INVALID_STRUCTURE"


def test_collar_skips_no_contract_when_window_empty():
    skip = build_overlay_plan(_sig(OverlayType.COLLAR), _snap(100), _rich_broker(),
                              _cfgN(), "s", _asof() + timedelta(days=400))
    assert isinstance(skip, OverlaySkip) and skip.reason == "SKIP_NO_CONTRACT"
