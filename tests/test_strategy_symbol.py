"""The internal strategy's symbol must be chosen deterministically.

`next(iter(frozenset))` depends on per-process string-hash ordering, so the strategy
silently traded a different holding on each restart (MARA/AAPL/IBIT/IAU). The pick must
be stable across restarts, with an explicit operator override.
"""
from autotrader.main import select_strategy_symbol


def test_picks_lexicographically_smallest_allowed_symbol():
    syms = frozenset({"US.MARA", "US.AAPL", "US.IBIT"})
    assert select_strategy_symbol(syms) == "US.AAPL"


def test_result_is_independent_of_set_construction_order():
    a = select_strategy_symbol(frozenset(["US.IBIT", "US.MARA", "US.AAPL"]))
    b = select_strategy_symbol(frozenset(["US.AAPL", "US.IBIT", "US.MARA"]))
    assert a == b == "US.AAPL"   # stable regardless of iteration/hash order


def test_explicit_override_wins_when_allowed():
    syms = frozenset({"US.MARA", "US.AAPL", "US.IBIT"})
    assert select_strategy_symbol(syms, override="US.IBIT") == "US.IBIT"


def test_override_is_normalized_case_and_whitespace():
    syms = frozenset({"US.MARA", "US.AAPL"})
    assert select_strategy_symbol(syms, override="  us.mara ") == "US.MARA"


def test_override_not_in_allowlist_falls_back_to_deterministic_pick():
    syms = frozenset({"US.MARA", "US.AAPL"})
    assert select_strategy_symbol(syms, override="US.TSLA") == "US.AAPL"


def test_empty_allowlist_falls_back_to_default():
    assert select_strategy_symbol(frozenset(), default="US.AAPL") == "US.AAPL"
