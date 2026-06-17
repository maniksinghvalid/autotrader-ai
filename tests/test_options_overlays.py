from autotrader.domain import OverlayType
from autotrader.options.overlays import REGISTRY, OverlayDef, LegSpec, ExitRule


def test_covered_call_is_single_short_call_requiring_shares():
    d = REGISTRY[OverlayType.COVERED_CALL]
    assert d.requires_underlying is True
    assert len(d.legs) == 1
    leg = d.legs[0]
    assert leg.right == "CALL" and leg.side == "SELL" and leg.position_effect == "OPEN"


def test_protective_put_is_single_long_put_requiring_shares():
    d = REGISTRY[OverlayType.PROTECTIVE_PUT]
    assert d.requires_underlying is True
    leg = d.legs[0]
    assert leg.right == "PUT" and leg.side == "BUY"


def test_every_registered_overlay_declares_legs():
    # CLAUDE.md hard rule: a strategy without an exit is rejected. O1 declares the
    # exit at plan time from config; here we assert structural completeness.
    for d in REGISTRY.values():
        assert isinstance(d, OverlayDef)
        assert len(d.legs) >= 1


def test_phantom_strategies_now_registered():
    for ov in (OverlayType.COLLAR, OverlayType.CALL_DIAGONAL,
               OverlayType.BEAR_PUT_SPREAD, OverlayType.LEAP):
        assert ov in REGISTRY


def test_collar_is_long_put_then_short_call_single_expiry():
    d = REGISTRY[OverlayType.COLLAR]
    assert d.requires_underlying is True and d.single_expiry is True
    assert [(l.right, l.side) for l in d.legs] == [("PUT", "BUY"), ("CALL", "SELL")]


def test_bear_put_spread_is_long_then_short_put_single_expiry():
    d = REGISTRY[OverlayType.BEAR_PUT_SPREAD]
    assert d.requires_underlying is False and d.single_expiry is True
    assert [(l.right, l.side) for l in d.legs] == [("PUT", "BUY"), ("PUT", "SELL")]
    assert d.legs[0].target_delta == 0.45 and d.legs[1].target_delta == 0.25


def test_call_diagonal_is_long_far_then_short_near_call():
    d = REGISTRY[OverlayType.CALL_DIAGONAL]
    assert d.requires_underlying is False and d.single_expiry is False
    assert [(l.right, l.side) for l in d.legs] == [("CALL", "BUY"), ("CALL", "SELL")]
    assert d.legs[0].dte_min == 180 and d.legs[1].dte_max == 45


def test_leap_is_single_long_call():
    d = REGISTRY[OverlayType.LEAP]
    assert d.requires_underlying is False and len(d.legs) == 1
    leg = d.legs[0]
    assert leg.right == "CALL" and leg.side == "BUY" and leg.dte_min == 180


def test_exit_rule_constructs_and_is_frozen():
    import dataclasses, pytest
    r = ExitRule(dte_to_close=21, profit_target_pct=0.5)
    assert r.close_on_reversal is True
    with pytest.raises(dataclasses.FrozenInstanceError):
        r.dte_to_close = 1


def test_legspec_rejects_bad_side():
    import pytest
    with pytest.raises(ValueError):
        LegSpec(right="CALL", side="sell")


def test_leap_enum_value_exists():
    assert OverlayType.LEAP.value == "LEAP"


def test_legspec_accepts_per_leg_targets():
    leg = LegSpec(right="CALL", side="BUY", target_delta=0.80, dte_min=180, dte_max=365)
    assert leg.target_delta == 0.80 and leg.dte_min == 180 and leg.dte_max == 365


def test_legspec_targets_default_to_none():
    leg = LegSpec(right="CALL", side="SELL")
    assert leg.target_delta is None and leg.dte_min is None and leg.dte_max is None


def test_legspec_rejects_bad_target_delta():
    import pytest
    with pytest.raises(ValueError):
        LegSpec(right="CALL", side="BUY", target_delta=1.5)
    with pytest.raises(ValueError):
        LegSpec(right="CALL", side="BUY", target_delta=0.0)


def test_legspec_rejects_inverted_dte():
    import pytest
    with pytest.raises(ValueError):
        LegSpec(right="CALL", side="BUY", dte_min=90, dte_max=30)


def test_overlaydef_single_expiry_defaults_false():
    d = OverlayDef(requires_underlying=True, legs=(LegSpec(right="CALL", side="SELL"),))
    assert d.single_expiry is False
