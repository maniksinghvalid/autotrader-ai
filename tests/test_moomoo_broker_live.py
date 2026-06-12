"""MoomooBroker tests require a live, authenticated OpenD paper session.
Run explicitly with:  RUN_LIVE=1 python -m pytest tests/test_moomoo_broker_live.py -v
They are skipped by default so the core suite stays offline/deterministic."""
import os
import pytest

pytestmark = pytest.mark.skipif(os.getenv("RUN_LIVE") != "1",
                                reason="set RUN_LIVE=1 with OpenD running (paper)")


def test_connect_and_account_snapshot_is_fresh():
    from autotrader.moomoo_broker import MoomooBroker
    b = MoomooBroker()
    b.connect()
    try:
        assert b.is_ready() is True
        snap = b.get_account()
        assert snap.stale is False
    finally:
        b.close()


def test_quote_round_trips():
    from autotrader.moomoo_broker import MoomooBroker
    b = MoomooBroker()
    b.connect()
    try:
        px = b.get_quote("US.AAPL")
        assert px is None or px > 0
    finally:
        b.close()
