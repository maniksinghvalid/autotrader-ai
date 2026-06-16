"""SessionRunner — the continuous, self-healing paper-trading loop.

Each iteration (run_once):
  1. poll the scheduler; run any newly-due lifecycle jobs (sync / open / sweep / flatten)
  2. ensure the broker connection is healthy (watchdog: backoff + reconcile-on-resume)
  3. if healthy, run one deterministic engine tick

run() loops run_once at the injected clock's time, sleeping loop_interval between
iterations, until the injected stop() predicate is True. The clock and sleep are
injected so the whole loop is testable without real time. Nothing here reaches the
broker except through the engine (which routes via risk_core -> OrderRouter) or the
explicit lifecycle jobs (sync / cancel_all)."""
from __future__ import annotations

import logging
from typing import Callable

from autotrader.clock import Clock
from autotrader.lifecycle import EntryGate, ground_truth_sync
from autotrader.scheduler import (
    LifecycleScheduler, PRE_OPEN_SYNC, ENTRY_OPEN, RISK_SWEEP, EOD_FLATTEN,
    REBALANCE, RISK_CHECK_MID, RISK_CHECK_LATE,
)

logger = logging.getLogger("autotrader.runner")


class SessionRunner:
    def __init__(self, engine, broker, db, gate: EntryGate,
                 scheduler: LifecycleScheduler, watchdog, clock: Clock,
                 sleep: Callable[[float], None], loop_interval: float = 5.0,
                 signal_inbox=None):
        self._engine = engine
        self._broker = broker
        self._db = db
        self._gate = gate
        self._sched = scheduler
        self._watch = watchdog
        self._clock = clock
        self._sleep = sleep
        self._loop_interval = loop_interval
        self._inbox = signal_inbox

    def _record_perf(self) -> None:
        snap = self._broker.get_account()
        self._db.record_performance(snap.day_pnl, snap.total_assets,
                                    snap.cash, snap.gross_exposure())

    def _run_job(self, job: str, now) -> None:
        if job == PRE_OPEN_SYNC:
            if self._broker is not None and self._db is not None:
                ground_truth_sync(self._broker, self._db)
        elif job == ENTRY_OPEN:
            self._gate.open()
            logger.info("ENTRY_OPEN: entries enabled")
        elif job == REBALANCE:
            self._engine.rebalance(now)
        elif job in (RISK_CHECK_MID, RISK_CHECK_LATE):
            self._engine.apply_risk_check(now)
        elif job == RISK_SWEEP:
            self._gate.close()
            if self._broker is not None and self._db is not None:
                ground_truth_sync(self._broker, self._db)
                self._record_perf()
            logger.info("RISK_SWEEP: entries closed, ground truth + performance recorded")
        elif job == EOD_FLATTEN:
            self._gate.close()
            if self._broker is not None:
                self._broker.cancel_all()
                self._record_perf()
            logger.info("EOD_FLATTEN: entries closed, all orders cancelled, performance committed")

    def run_once(self, now) -> str:
        """Execute one loop iteration. Returns the engine tick action, or
        'HALTED' if the gate is halted, or 'HALTED_UNHEALTHY' if the watchdog
        could not restore the connection. External inbox signals are routed only
        when healthy."""
        for job in self._sched.poll(now):
            self._run_job(job, now)
        if self._gate is not None and self._gate.halted:
            return "HALTED"
        if not self._watch.ensure_healthy():
            return "HALTED_UNHEALTHY"
        action = self._engine.tick().action
        if self._inbox is not None:
            for sig in self._inbox.poll():
                res = self._engine.submit_external_signal(sig)
                logger.debug("external signal %s -> %s", sig.symbol, res.action)
        return action

    def run(self, stop: Callable[[], bool]) -> None:
        logger.info("SessionRunner started (loop_interval=%.1fs)", self._loop_interval)
        while not stop():
            try:
                action = self.run_once(self._clock.now_est())
                logger.debug("run_once -> %s", action)
            except Exception as e:  # never let one bad iteration kill the session
                logger.error("run_once error: %s", e)
            self._sleep(self._loop_interval)
        logger.info("SessionRunner stopped")
