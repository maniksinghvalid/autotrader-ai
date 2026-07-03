from autotrader import books


def test_constants_have_expected_values():
    assert books.ORIGIN_BREAKOUT == "BREAKOUT"
    assert books.SKIP_BOOK_CONFLICT == "BOOK_CONFLICT"
    assert books.SKIP_BOOK_CLAIMED == "BOOK_CLAIMED"
    assert books.SKIP_POSITION_NOT_OWNED == "POSITION_NOT_OWNED"


def test_is_breakout_claimed_true_only_for_breakout_origin():
    claims = {"US.NIO": "BREAKOUT"}
    assert books.is_breakout_claimed("US.NIO", claims) is True


def test_is_breakout_claimed_false_when_absent():
    assert books.is_breakout_claimed("US.NIO", {}) is False


def test_is_breakout_claimed_false_for_other_origin():
    assert books.is_breakout_claimed("US.NIO", {"US.NIO": "OTHER"}) is False
