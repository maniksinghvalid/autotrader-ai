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


def test_unsupported_overlays_absent_from_registry():
    assert OverlayType.COLLAR not in REGISTRY
    assert OverlayType.CALL_DIAGONAL not in REGISTRY
    assert OverlayType.BEAR_PUT_SPREAD not in REGISTRY
