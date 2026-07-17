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
from datetime import date as _date, timedelta
from typing import Callable, Optional

from autotrader.clock import Clock
from autotrader.lifecycle import EntryGate, ground_truth_sync
from autotrader.day_pnl import unrealized_from_quotes
from autotrader.reporting.pnl import realized_from_fills
from autotrader.scheduler import (
    LifecycleScheduler, PRE_OPEN_SYNC, ENTRY_OPEN, RISK_SWEEP, EOD_CANCEL_ORDERS,
    REBALANCE, RISK_CHECK_MID, RISK_CHECK_LATE, EOD_REPORT,
)
from autotrader.watchdog import backoff_seconds

logger = logging.getLogger("autotrader.runner")


class SessionRunner:
    def __init__(self, engine, broker, db, gate: EntryGate,
                 scheduler: LifecycleScheduler, watchdog, clock: Clock,
                 sleep: Callable[[float], None], loop_interval: float = 5.0,
                 signal_inbox=None, reporter=None, stop_manager=None,
                 trading_day_fn: Optional[Callable[[_date], bool]] = None,
                 alerts=None, owned_only: bool = False,
                 heartbeat: Optional[Callable[[], None]] = None,
                 heartbeat_every: int = 60,
                 simstop_interval: float = 60.0):
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
        self._reporter = reporter
        self._stop_manager = stop_manager
        self._trading_day_fn = trading_day_fn
        self._alerts = alerts
        # V8c: dead-man's-switch heartbeat — an external monitor (e.g. a cron-hit
        # URL / healthchecks.io) that fires every heartbeat_every iterations so an
        # outside watcher can detect a silently-wedged process. Never allowed to
        # kill the loop (see run()).
        self._heartbeat = heartbeat
        self._heartbeat_every = heartbeat_every
        # V11/C6: SHARED-mode scoping — fills/positions ingestion is restricted
        # to AutoTrader's own tracked book (see lifecycle.ground_truth_sync).
        self._owned_only = owned_only
        # V8b: tracks whether we are mid-episode of watchdog exhaustion, so the
        # alert (and db.record_halt) fires ONCE per episode, not every 5s
        # iteration while unhealthy.
        self._unhealthy_episode = False
        # Simulated trailing stops (paper): sweep interval + next-due marker.
        self._simstop_interval = simstop_interval
        self._simstop_next = None

    def _fetch_account_for_perf(self, max_attempts: int = 4):
        """Fetch the account snapshot for the performance row, retrying with
        capped exponential backoff while the position query keeps failing
        (positions_loaded is False). Returns the first loaded snapshot, or the
        last snapshot after exhausting attempts. Injected sleep — never a bare
        time.sleep as a readiness check (CLAUDE.md)."""
        snap = self._broker.get_account()
        attempt = 1
        while not snap.positions_loaded and attempt < max_attempts:
            delay = backoff_seconds(attempt)
            logger.warning("record_perf: positions unloaded — backoff %.1fs "
                           "(attempt %d/%d)", delay, attempt, max_attempts)
            self._sleep(delay)
            snap = self._broker.get_account()
            attempt += 1
        return snap

    def _compute_unrealized(self, snap) -> float:
        """Σ qty × (current_quote − avg_cost) over open positions, marked from live
        quotes (shared with the engine risk check so the two cannot drift)."""
        return unrealized_from_quotes(snap.positions, self._broker.get_quote)

    def _compute_realized(self) -> "float | None":
        rows = self._db._conn.execute(
            "SELECT symbol, side, qty, price, ts FROM fills").fetchall()
        return realized_from_fills(rows, _date.today().isoformat())

    def _record_perf(self) -> None:
        snap = self._fetch_account_for_perf()
        gross = snap.gross_exposure() if snap.positions_loaded else None
        # Realized: prefer our own fills-derived figure; fall back to broker day_pnl.
        realized = self._compute_realized()
        day_pnl = realized if realized is not None else snap.day_pnl
        # Unrealized: recompute from positions × quote only when positions loaded;
        # otherwise keep the broker figure (reporter renders it 'unavailable').
        unreal = self._compute_unrealized(snap) if snap.positions_loaded \
            else snap.unrealized_pnl
        self._db.record_performance(
            day_pnl, snap.total_assets, snap.cash, gross, unreal,
            positions_loaded=snap.positions_loaded)

    def _run_job(self, job: str, now) -> None:
        if job == PRE_OPEN_SYNC:
            if self._broker is not None and self._db is not None:
                ground_truth_sync(self._broker, self._db, owned_only=self._owned_only)
                if self._engine is not None:
                    self._engine.reconcile_claims()
        elif job == ENTRY_OPEN:
            self._gate.open()
            logger.info("ENTRY_OPEN: entries enabled")
            # W1: reconcile protective stops FIRST (EOD cancelled them; DAY TIF
            # would have lapsed them anyway), then replay deferred entries —
            # whose own stops attach at entry.
            if self._stop_manager is not None:
                self._stop_manager.reconcile(now.date())
            # Replay any external BUYs that arrived pre-market (deferred, not dropped)
            # now that the window is open — routed against a fresh snapshot/quote.
            if self._engine is not None:
                self._engine.flush_deferred_entries()
        elif job == REBALANCE:
            self._engine.rebalance(now)
        elif job in (RISK_CHECK_MID, RISK_CHECK_LATE):
            self._engine.apply_risk_check(now)
            # W7 fix: sync fills BEFORE recording perf (mirrors RISK_SWEEP/EOD) so a HALT
            # during this job — which skips RISK_SWEEP/EOD for the rest of the day — doesn't
            # leave the day's LAST performance row computed off stale/under-counted fills.
            if self._broker is not None and self._db is not None:
                ground_truth_sync(self._broker, self._db, owned_only=self._owned_only)
                self._engine.reconcile_claims()
                self._record_perf()
            # V3b: intraday stop backstop — any position whose entry-time stop
            # attach was unconfirmed (C2) is protected within hours, not next
            # morning. Skipped when halted: a flattened book needs no stops.
            if (self._stop_manager is not None
                    and not (self._gate is not None and self._gate.halted)):
                self._stop_manager.reconcile(now.date())
        elif job == RISK_SWEEP:
            self._gate.close()
            if self._broker is not None and self._db is not None:
                ground_truth_sync(self._broker, self._db, owned_only=self._owned_only)
                self._engine.reconcile_claims()
                self._record_perf()
            # V3b: intraday stop backstop — any position whose entry-time stop
            # attach was unconfirmed (C2) is protected within hours, not next
            # morning. Skipped when halted: a flattened book needs no stops.
            if (self._stop_manager is not None
                    and not (self._gate is not None and self._gate.halted)):
                self._stop_manager.reconcile(now.date())
            logger.info("RISK_SWEEP: entries closed, ground truth + performance recorded")
        elif job == EOD_CANCEL_ORDERS:
            self._gate.close()
            if self._broker is not None:
                self._engine.cancel_working_orders()
                if self._db is not None:   # I6: late fills (15:30-16:30) must land
                    ground_truth_sync(self._broker, self._db, owned_only=self._owned_only)
                self._record_perf()
            logger.info("EOD_CANCEL_ORDERS: entries closed, working orders cancelled "
                        "(stops re-attach at next ENTRY_OPEN), performance committed")
        elif job == EOD_REPORT:
            if self._reporter is not None:
                self._reporter.send_eod_report(now)
                logger.info("EOD_REPORT: session summary sent to Slack")

    def run_once(self, now) -> str:
        """Execute one loop iteration. Returns the engine tick action, or
        'HALTED' if the gate is halted, or 'HALTED_UNHEALTHY' if the watchdog
        could not restore the connection. External inbox signals are routed only
        when healthy. On a non-trading day (weekend/holiday, when a
        trading_day_fn is wired) NOTHING runs — no lifecycle jobs, no tick, no
        inbox — and 'NON_TRADING_DAY' is returned."""
        if self._trading_day_fn is not None and not self._trading_day_fn(now.date()):
            return "NON_TRADING_DAY"
        for job in self._sched.poll(now):
            try:
                self._run_job(job, now)
            except Exception as e:
                logger.error("lifecycle job %s failed: %s", job, e, exc_info=True)
                if self._alerts is not None:
                    self._alerts.send(
                        f"⚠ lifecycle job {job} FAILED: {e} — will retry next poll",
                        key=f"job-fail:{job}:{now.date().isoformat()}")
                continue     # later due jobs still run; this one retries next poll
            self._sched.mark_fired(job, now)
        if self._gate is not None and self._gate.halted:
            return "HALTED"
        if not self._watch.ensure_healthy():
            if not self._unhealthy_episode:
                self._unhealthy_episode = True
                logger.error("watchdog exhausted — connection not restored")
                if self._alerts is not None:
                    self._alerts.send(
                        "⚠ TRADER UNHEALTHY — watchdog could not restore the "
                        "OpenD connection; loop is idling. Check OpenD.",
                        key="watchdog-unhealthy")
                if self._db is not None:
                    self._db.record_halt("watchdog unhealthy: connection not restored")
            return "HALTED_UNHEALTHY"
        if self._unhealthy_episode:
            self._unhealthy_episode = False
            if self._alerts is not None:
                self._alerts.reset("watchdog-unhealthy")
        # Simulated trailing stops (paper): protective exits run BEFORE new
        # signals. Session-gated by entries_enabled (ENTRY_OPEN..RISK_SWEEP);
        # the halted path already returned above.
        # ponytail: sweep stops at 15:30 with the entry gate; live broker stops
        # rest until EOD cancel — add a session predicate if the gap ever matters
        if (self._stop_manager is not None and self._gate is not None
                and self._gate.entries_enabled
                and (self._simstop_next is None or now >= self._simstop_next)):
            self._stop_manager.check_simulated(now)
            self._simstop_next = now + timedelta(seconds=self._simstop_interval)
        action = self._engine.tick().action
        if self._inbox is not None:
            for sig in self._inbox.poll():
                res = self._engine.submit_external_signal(sig)
                logger.debug("external signal %s -> %s", sig.symbol, res.action)
        return action

    def run(self, stop: Callable[[], bool]) -> None:
        logger.info("SessionRunner started (loop_interval=%.1fs)", self._loop_interval)
        errors = 0
        it = 0
        while not stop():
            try:
                action = self.run_once(self._clock.now_est())
                logger.debug("run_once -> %s", action)
                errors = 0
                if self._alerts is not None:
                    self._alerts.reset("loop-errors")
            except Exception as e:  # never let one bad iteration kill the session
                errors += 1
                logger.error("run_once error: %s", e, exc_info=True)
                if errors >= 5 and self._alerts is not None:
                    self._alerts.send(
                        f"⚠ TRADER DEGRADED — {errors} consecutive loop errors; "
                        f"latest: {e}", key="loop-errors")
            it += 1
            if self._heartbeat is not None and (it - 1) % self._heartbeat_every == 0:
                try:
                    self._heartbeat()
                except Exception as e:    # dead-man ping must never kill the loop
                    logger.warning("heartbeat failed: %s", e)
            self._sleep(self._loop_interval)
        logger.info("SessionRunner stopped")
