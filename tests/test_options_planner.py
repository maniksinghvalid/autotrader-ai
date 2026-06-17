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
