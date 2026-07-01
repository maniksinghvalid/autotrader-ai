"""EOD retry-with-backoff on a stale/failed positions snapshot.

_record_perf() must retry the account fetch with capped exponential backoff
(watchdog.backoff_seconds, injected sleep) while positions_loaded is False,
and persist gross_exposure=NULL + positions_loaded=False if still unresolved
after exhausting attempts. No real sleeping — sleep is injected."""
from autotrader.db import DB
from autotrader.domain import AccountSnapshot, Position
from autotrader.runner import SessionRunner


class _AcctStub:
    """Broker stub: returns not-loaded snapshots for `fail_times` calls, then loaded."""
    def __init__(self, fail_times, loaded_positions=(), quotes=None):
        self.fail_times = fail_times
        self.calls = 0
        self._pos = tuple(loaded_positions)
        self._quotes = quotes or {}

    def get_account(self):
        self.calls += 1
        if self.calls <= self.fail_times:
            return AccountSnapshot(cash=1000.0, total_assets=1000.0, day_pnl=0.0,
                                    stale=True, positions_loaded=False, positions=())
        return AccountSnapshot(cash=1000.0, total_assets=1000.0, day_pnl=0.0,
                                stale=False, positions_loaded=True, positions=self._pos)

    def get_quote(self, symbol):
        return self._quotes.get(symbol)


def _runner(db, broker, sleeps):
    return SessionRunner(engine=None, broker=broker, db=db, gate=None,
                          scheduler=None, watchdog=None, clock=None,
                          sleep=sleeps.append, loop_interval=5.0)


def test_record_perf_retries_then_records_real_gross(tmp_path):
    db = DB(str(tmp_path / "r.db"))
    br = _AcctStub(fail_times=2, loaded_positions=[Position("US.AAPL", 10, 100.0)],
                   quotes={"US.AAPL": 100.0})
    sleeps = []
    _runner(db, br, sleeps)._record_perf()
    assert br.calls == 3                      # 2 failed + 1 success
    assert sleeps == [1.0, 2.0]               # backoff before each retry
    row = db._conn.execute(
        "SELECT gross_exposure FROM performance").fetchone()
    assert row[0] == 1000.0                    # abs(10)*100 avg_price
    db.close()


def test_record_perf_records_null_gross_when_never_resolved(tmp_path):
    db = DB(str(tmp_path / "r.db"))
    br = _AcctStub(fail_times=99)
    sleeps = []
    _runner(db, br, sleeps)._record_perf()
    assert br.calls == 4                       # 1 initial + 3 retries (max_attempts=4)
    assert sleeps == [1.0, 2.0, 4.0]
    row = db._conn.execute(
        "SELECT gross_exposure FROM performance").fetchone()
    assert row[0] is None
    db.close()
