from autotrader.options.overlays import (
    ExitRule, LegSpec, OverlayDef, _UNSET,
)


def test_legspec_prefer_longest_defaults_false():
    leg = LegSpec(right="CALL", side="BUY")
    assert leg.prefer_longest is False


def test_legspec_prefer_longest_settable():
    leg = LegSpec(right="CALL", side="BUY", prefer_longest=True)
    assert leg.prefer_longest is True


def test_overlaydef_exit_overrides_default_unset():
    d = OverlayDef(requires_underlying=False, legs=(LegSpec(right="CALL", side="BUY"),))
    assert d.dte_to_close is _UNSET
    assert d.profit_target_pct is _UNSET


def test_overlaydef_exit_overrides_accept_none_and_values():
    d = OverlayDef(requires_underlying=False,
                   legs=(LegSpec(right="CALL", side="BUY"),),
                   dte_to_close=120, profit_target_pct=None)
    assert d.dte_to_close == 120
    assert d.profit_target_pct is None


def test_exitrule_profit_target_optional():
    r = ExitRule(dte_to_close=120, profit_target_pct=None)
    assert r.profit_target_pct is None
