"""Orchestration. One deterministic tick: snapshot -> quote -> strategy ->
confidence filter -> risk core -> router. Shutdown cancels all open orders
before disconnect (CLAUDE.md). The engine takes any Broker, so it is testable
against SimBroker with no OpenD. The __main__ path wires a live MoomooBroker
behind is_opend_ready()."""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date
from typing import TYPE_CHECKING, Optional

from autotrader.broker import Broker
from autotrader.config import RiskConfig, load_risk_config
from autotrader.domain import OrderRequest, OrderState, Signal
from autotrader.rebalance import compute_plan
from autotrader.risk_check import evaluate as risk_evaluate, RiskAction
from autotrader.risk_core import evaluate
from autotrader.router import OrderRouter
from autotrader.sizing import size_position
from autotrader.strategies.threshold import ThresholdStrategy

if TYPE_CHECKING:
    from autotrader.db import DB
    from autotrader.lifecycle import EntryGate

logger = logging.getLogger("autotrader.engine")


@dataclass(frozen=True)
class TickResult:
    action: str
    detail: str = ""


class TradeEngine:
    def __init__(self, broker: Broker, strategy: ThresholdStrategy, cfg: RiskConfig,
                 order_qty: int, audit_path: str, db: "Optional[DB]" = None,
                 entry_gate: "Optional[EntryGate]" = None, today_fn=None):
        self._b = broker
        self._strat = strategy
        self._cfg = cfg
        self._qty = order_qty
        self._router = OrderRouter(broker, audit_path=audit_path)
        self._signal_seq = 0
        self._db = db
        self._gate = entry_gate
        self._today_fn = today_fn or date.today

    def tick(self) -> TickResult:
        if self._gate is not None and self._gate.halted:
            return TickResult("HALTED")
        snap = self._b.get_account()
        symbol = self._strat.p.symbol
        price = self._b.get_quote(symbol)
        if price is None:
            return TickResult("NO_QUOTE", symbol)

        pos = next((p for p in snap.positions if p.symbol == symbol), None)
        signal = self._strat.evaluate(price=price, position=pos)
        if signal is None:
            return TickResult("NO_SIGNAL")
        return self._route_signal(signal, snap, price)

    def submit_external_signal(self, signal: Signal) -> TickResult:
        """Route a validated external signal through the SAME pipeline as a
        strategy signal: confidence filter -> entry gate -> risk core -> router.
        External signals NEVER bypass the risk core (research C4)."""
        snap = self._b.get_account()
        price = self._b.get_quote(signal.symbol)
        if price is None:
            return TickResult("NO_QUOTE", signal.symbol)
        return self._route_signal(signal, snap, price)

    def _route_signal(self, signal: Signal, snap, price: float) -> TickResult:
        if signal.confidence < self._cfg.min_confidence:
            return TickResult("DROPPED_LOW_CONFIDENCE", f"{signal.confidence}")

        # Entry-window gate: block NEW entries (BUY) when closed; exits (SELL)
        # are never gated — you must always be able to flatten.
        if (signal.direction == "BUY" and self._gate is not None
                and not self._gate.entries_enabled):
            return TickResult("ENTRY_CLOSED", signal.symbol)

        # Option overlays expand into leg orders through this SAME audited path.
        if signal.overlay is not None:
            return self._route_overlay(signal, snap)

        if self._db:
            self._db.record_performance(
                day_pnl=snap.day_pnl,
                total_assets=snap.total_assets,
                cash=snap.cash,
                gross_exposure=snap.gross_exposure(),
            )

        self._signal_seq += 1
        signal_id = f"sig-{self._signal_seq}"
        if self._db:
            self._db.record_signal(
                symbol=signal.symbol, direction=signal.direction,
                confidence=signal.confidence, rationale=signal.rationale,
                signal_id=signal_id,
            )

        # SELL exits liquidate the full position. BUY entries are risk-sized off the
        # stop distance (autotrader.sizing); with sizing disabled this returns the
        # configured order_qty unchanged. The sizer only proposes/clamps-down — the
        # risk core below remains the sole pass/reject gate.
        pos = next((p for p in snap.positions if p.symbol == signal.symbol), None)
        if signal.direction == "SELL" and pos is not None:
            eff_qty = pos.qty
        else:
            sr = size_position(
                equity=snap.total_assets, entry_price=price,
                signal_stop=signal.stop_price, confidence=signal.confidence,
                cfg=self._cfg, current_qty=(pos.qty if pos else 0),
                gross_exposure=snap.gross_exposure(), fixed_qty=self._qty)
            if sr.used_risk_sizing and sr.qty <= 0:
                logger.info("signal %s sized to 0 (%s) — not placing", signal_id, sr.reason)
                return TickResult("SIZED_ZERO", f"{signal.symbol}:{sr.reason}")
            if sr.reason == "NO_STOP_DISTANCE_FALLBACK":
                logger.warning("no stop distance for %s; falling back to fixed qty %d",
                               signal.symbol, self._qty)
            eff_qty = sr.qty
        cid = OrderRouter.make_client_order_id(signal.symbol, signal.direction, eff_qty, signal_id)
        req = OrderRequest(symbol=signal.symbol, side=signal.direction, qty=eff_qty,
                           order_type="MARKET", limit_price=None, client_order_id=cid)

        decision = evaluate(req, snap, self._cfg, ref_price=price)
        if not decision.approved:
            logger.warning("risk core rejected: %s", decision.reason)
            return TickResult("REJECTED_BY_RISK", decision.reason)

        ack = self._router.submit(req)
        if self._db:
            self._db.record_trade(
                client_order_id=ack.client_order_id, symbol=req.symbol, side=req.side,
                qty=req.qty, order_type=req.order_type, limit_price=req.limit_price,
                broker_order_id=ack.broker_order_id, state=ack.state.value,
            )
        if ack.state is OrderState.UNKNOWN:
            return TickResult("ORDER_UNKNOWN", ack.client_order_id)
        if ack.state is OrderState.REJECTED:
            return TickResult("ORDER_REJECTED", ack.client_order_id)

        # Broker-resting trailing stop: attach a protective TRAILING_STOP SELL
        # right after a BUY entry places (research R5 — survives an OpenD outage).
        if signal.direction == "BUY" and self._cfg.trailing_stop_pct > 0:
            self._attach_trailing_stop(signal.symbol, eff_qty, price, signal_id)
        return TickResult("ORDER_PLACED", str(ack.broker_order_id))

    def _route_overlay(self, signal: Signal, snap) -> TickResult:
        """Expand an overlay signal into legs and place each through the audited
        router. Legs are ordered long-before-short so a covered structure's hedge
        is never momentarily naked. O1 overlays are single-leg; multi-leg
        atomic-unwind on partial failure is O2."""
        # lazy import: keeps the options subpackage optional (matches rebalance()/_flatten_all())
        from autotrader.options.planner import build_overlay_plan, OverlayPlan

        self._signal_seq += 1
        signal_id = f"sig-{self._signal_seq}"
        plan = build_overlay_plan(signal, snap, self._b, self._cfg,
                                  signal_id, self._today_fn())
        if not isinstance(plan, OverlayPlan):
            logger.info("overlay skipped %s %s: %s",
                        plan.overlay.value, plan.underlying, plan.reason)
            return TickResult(plan.reason, f"{plan.overlay.value}:{plan.underlying}")

        # O1: overlays record signal+trade but not a performance snapshot (they adjust an existing position, not a standalone P&L event).
        if self._db:
            self._db.record_signal(
                symbol=signal.symbol, direction=signal.direction,
                confidence=signal.confidence,
                rationale=f"{plan.overlay.value}: {signal.rationale}",
                signal_id=signal_id)

        # Entry-window gate: block OPEN overlay legs when entries are closed.
        # Mirrors the BUY-entry gate in _route_signal. CLOSE legs (buy-to-close,
        # sell-to-close) are exits and must never be gated — only reject if the
        # plan contains at least one OPEN leg.
        if (self._gate is not None and not self._gate.entries_enabled
                and any(l.request.position_effect == "OPEN" for l in plan.legs)):
            return TickResult("ENTRY_CLOSED", f"{plan.overlay.value}:{plan.underlying}")

        # Defined-risk coverage context: the plan's long OPEN legs cover its short
        # legs (the risk core recognizes this). Long legs are submitted first, so a
        # short is only ever placed after its cover is acked.
        coverage = tuple(l.request for l in plan.legs
                         if l.request.side == "BUY" and l.request.position_effect == "OPEN")

        last_boid = None
        filled_long = []  # symbols of long legs already filled this overlay

        def _residual(failed_symbol: str, why: str) -> TickResult:
            logger.warning("overlay %s left long-only residual %s after %s on %s",
                           plan.correlation_id, filled_long, why, failed_symbol)
            return TickResult("OVERLAY_RESIDUAL_LONG",
                              f"{plan.correlation_id}:{','.join(filled_long)}")

        for leg in sorted(plan.legs, key=lambda l: 0 if l.request.side == "BUY" else 1):
            req = leg.request
            decision = evaluate(req, snap, self._cfg, ref_price=leg.quote.premium,
                                coverage_legs=coverage)
            if not decision.approved:
                logger.warning("overlay leg rejected (%s): %s", req.symbol, decision.reason)
                if filled_long:
                    return _residual(req.symbol, decision.reason)
                return TickResult("REJECTED_BY_RISK", decision.reason)
            ack = self._router.submit(req)
            if self._db:
                self._db.record_trade(
                    client_order_id=ack.client_order_id, symbol=req.symbol,
                    side=req.side, qty=req.qty, order_type=req.order_type,
                    limit_price=leg.quote.premium, broker_order_id=ack.broker_order_id,
                    state=ack.state.value)
            if ack.state in (OrderState.UNKNOWN, OrderState.REJECTED):
                if filled_long:
                    return _residual(req.symbol, ack.state.value)
                action = "ORDER_UNKNOWN" if ack.state is OrderState.UNKNOWN else "ORDER_REJECTED"
                return TickResult(action, ack.client_order_id)
            if (req.side == "BUY" and req.position_effect == "OPEN"
                    and ack.state is OrderState.FILLED):
                filled_long.append(req.symbol)
            last_boid = ack.broker_order_id
        return TickResult("OVERLAY_PLACED", f"{plan.correlation_id}:{last_boid}")

    def _attach_trailing_stop(self, symbol: str, qty: int, ref_price: float,
                              entry_signal_id: str) -> None:
        """Place a broker-resting TRAILING_STOP SELL for `qty` shares through the
        SAME audited risk path. Idempotent: the client_order_id is derived from
        the entry's signal id, so re-attaching for the same entry dedupes at the
        router. A rejected stop is logged, never fatal to the entry. Stop
        consolidation on qty changes (pyramiding) is Phase 3.

        LIVE NOTE (fail-safe): against SimBroker the BUY auto-fills, so the
        re-fetched snapshot reflects the new position and the stop attaches. On
        live OpenD a MARKET BUY returns SUBMITTED (async fill), so this snapshot
        may still show the pre-entry position; the risk core then rejects the
        stop as long-only (resulting < 0) and it simply does not attach this tick
        — the entry is left unprotected but NEVER mis-directed (no short can
        open). Attaching off a reconciled fill is a Phase 3 follow-up."""
        snap = self._b.get_account()  # SimBroker: reflects the just-filled position (see LIVE NOTE)
        cid = OrderRouter.make_client_order_id(symbol, "SELL", qty, f"{entry_signal_id}-stop")
        req = OrderRequest(symbol=symbol, side="SELL", qty=qty, order_type="TRAILING_STOP",
                           limit_price=None, client_order_id=cid,
                           trail_percent=self._cfg.trailing_stop_pct)
        decision = evaluate(req, snap, self._cfg, ref_price=ref_price)
        if not decision.approved:
            logger.warning("trailing stop NOT attached for %s: %s", symbol, decision.reason)
            return
        ack = self._router.submit(req)
        if self._db:
            self._db.record_trade(
                client_order_id=ack.client_order_id, symbol=req.symbol, side=req.side,
                qty=req.qty, order_type=req.order_type, limit_price=req.limit_price,
                broker_order_id=ack.broker_order_id, state=ack.state.value,
            )
        logger.info("trailing stop attached: %s SELL %d @ %.1f%% trail",
                    symbol, qty, self._cfg.trailing_stop_pct)

    def submit_rebalance_order(self, trade, ref_price: float, round_id: str):
        """Route a rebalance trim/top-up through the SAME audited path as any
        order: risk_core -> OrderRouter -> db. Honors the entry gate for BUY
        top-ups and the session halt; allows EXPLICIT partial SELL qty (it does
        not apply tick()'s 'SELL = full position' shortcut)."""
        if self._gate is not None and self._gate.halted:
            return TickResult("HALTED", trade.symbol)
        if (trade.side == "BUY" and self._gate is not None
                and not self._gate.entries_enabled):
            return TickResult("ENTRY_CLOSED", trade.symbol)
        snap = self._b.get_account()
        cid = OrderRouter.make_client_order_id(
            trade.symbol, trade.side, trade.qty, f"{round_id}-rbal")
        req = OrderRequest(symbol=trade.symbol, side=trade.side, qty=trade.qty,
                           order_type="MARKET", limit_price=None,
                           client_order_id=cid)
        decision = evaluate(req, snap, self._cfg, ref_price=ref_price)
        if not decision.approved:
            logger.warning("rebalance rejected %s %s %d: %s", trade.side,
                           trade.symbol, trade.qty, decision.reason)
            return TickResult("REJECTED_BY_RISK", decision.reason)
        ack = self._router.submit(req)
        if self._db:
            self._db.record_trade(
                client_order_id=ack.client_order_id, symbol=req.symbol,
                side=req.side, qty=req.qty, order_type=req.order_type,
                limit_price=req.limit_price, broker_order_id=ack.broker_order_id,
                state=ack.state.value)
        if ack.state is OrderState.UNKNOWN:
            return TickResult("ORDER_UNKNOWN", ack.client_order_id)
        if ack.state is OrderState.REJECTED:
            return TickResult("ORDER_REJECTED", ack.client_order_id)
        if self._db:
            # Rebalance/flatten trades carry no domain.Signal; leave a driver row so
            # the EOD report can explain them (round_id prefix distinguishes a
            # rebalance reconciliation from a loss-halt liquidation).
            kind = "liquidation" if round_id.startswith("halt") else "rebalance"
            reason = {"TRIM": "overweight → trim",
                      "TOPUP": "underweight → top-up"}.get(trade.action, trade.action.lower())
            self._db.record_driver(trade.symbol, trade.side, kind, f"{round_id} · {reason}")
        return TickResult("ORDER_PLACED", str(ack.broker_order_id))

    def consolidate_stop(self, symbol: str, new_total_qty: int,
                         ref_price: float, round_id: str) -> None:
        """Re-size the protective trailing stop after a qty change: cancel the
        symbol's working TRAILING_STOP, then (if new_total_qty > 0) place a fresh
        one for the full intended qty through the audited path. new_total_qty is
        the deterministic intended post-trade qty (NOT a re-fetched snapshot), so
        a live async fill cannot under-size the stop. The RISK eval re-fetches the
        snapshot (like _attach_trailing_stop): on live OpenD a not-yet-filled
        top-up makes the stop fail long-only and simply not attach this round."""
        if self._db is None or self._cfg.trailing_stop_pct <= 0:
            return
        existing = self._db.get_open_trailing_stop(symbol)
        if existing is not None:
            try:
                self._b.cancel_order(existing)
            except Exception as e:
                logger.error("consolidate_stop: cancel %s failed: %s", existing, e)
                raise
            self._db.mark_order_cancelled(existing)
        if new_total_qty <= 0:
            return
        snap = self._b.get_account()
        cid = OrderRouter.make_client_order_id(
            symbol, "SELL", new_total_qty, f"{round_id}-stop")
        req = OrderRequest(symbol=symbol, side="SELL", qty=new_total_qty,
                           order_type="TRAILING_STOP", limit_price=None,
                           client_order_id=cid,
                           trail_percent=self._cfg.trailing_stop_pct)
        decision = evaluate(req, snap, self._cfg, ref_price=ref_price)
        if not decision.approved:
            logger.warning("consolidated stop NOT attached for %s: %s",
                           symbol, decision.reason)
            return
        ack = self._router.submit(req)
        self._db.record_trade(
            client_order_id=ack.client_order_id, symbol=req.symbol, side=req.side,
            qty=req.qty, order_type=req.order_type, limit_price=req.limit_price,
            broker_order_id=ack.broker_order_id, state=ack.state.value)
        logger.info("stop consolidated: %s SELL %d @ %.1f%% trail",
                    symbol, new_total_qty, self._cfg.trailing_stop_pct)

    def rebalance(self, now) -> str:
        """Midday rebalance: load the latest target snapshot, skip if disabled /
        absent / stale, else compute a drift-band plan and execute each trade
        through the audited path, consolidating the trailing stop per qty change.
        `now` is the market-time clock (drives the staleness guard)."""
        from datetime import datetime, timedelta
        if not self._cfg.rebalance_enabled:
            return "REBALANCE_DISABLED"
        if self._gate is not None and self._gate.halted:
            return "HALTED"
        if self._db is None:
            return "NO_TARGETS"
        latest = self._db.latest_target_weights()
        if latest is None:
            return "NO_TARGETS"
        as_of, ingested_at, scores = latest
        age = now - datetime.fromisoformat(ingested_at)
        if age > timedelta(hours=self._cfg.target_staleness_hours):
            logger.info("rebalance: targets stale (age=%s) — skipping", age)
            return "STALE_TARGETS"

        snap = self._b.get_account()
        prices = {}
        for sym in scores:
            q = self._b.get_quote(sym)
            if q is not None:
                prices[sym] = q
        plan = compute_plan(snap, scores, prices, self._cfg)
        round_id = f"rbal-{as_of}"
        for sym, reason in plan.skipped:
            logger.debug("rebalance skip %s: %s", sym, reason)
        for trade in plan.trades:
            res = self.submit_rebalance_order(trade, prices[trade.symbol], round_id)
            if res.action == "ORDER_PLACED":
                self.consolidate_stop(trade.symbol, trade.new_total_qty,
                                      prices[trade.symbol], round_id)
            else:
                logger.info("rebalance %s %s -> %s", trade.side, trade.symbol,
                            res.action)
        logger.info("REBALANCE complete: %d trade(s), %d skipped",
                    len(plan.trades), len(plan.skipped))
        return "REBALANCED"

    def _flatten_all(self, snapshot, round_id: str) -> None:
        """Liquidate every long position through the audited path. Called BEFORE
        the halt flag is set, so the SELLs are not blocked by the halt guard."""
        from autotrader.rebalance import RebalanceTrade
        for p in snapshot.positions:
            if p.qty <= 0:
                continue
            price = self._b.get_quote(p.symbol) or p.avg_price
            trade = RebalanceTrade(p.symbol, "SELL", p.qty, "TRIM", 0)
            res = self.submit_rebalance_order(trade, price, round_id)
            logger.info("flatten %s qty=%d -> %s", p.symbol, p.qty, res.action)

    def apply_risk_check(self, now) -> str:
        """Tiered intraday preservation. GATE: close entries, keep positions +
        stops. HALT: flatten all, cancel all, record the halt, set the session
        halt flag. Returns the RiskAction name."""
        snap = self._b.get_account()
        action = risk_evaluate(snap, self._cfg)
        if self._db:
            self._db.record_performance(
                day_pnl=snap.day_pnl, total_assets=snap.total_assets,
                cash=snap.cash, gross_exposure=snap.gross_exposure())
        if action is RiskAction.GATE:
            if self._gate is not None:
                self._gate.close()
            logger.warning("RISK_CHECK soft breach: entries closed (pnl=%.2f)",
                           snap.day_pnl)
        elif action is RiskAction.HALT:
            reason = f"daily loss halt: pnl={snap.day_pnl}"
            round_id = f"halt-{now.date().isoformat()}"
            self._flatten_all(snap, round_id)   # BEFORE halt flag (guard would block)
            try:
                self._b.cancel_all()
            except Exception as e:
                logger.error("RISK_CHECK halt: cancel_all failed: %s", e)
            if self._gate is not None:
                self._gate.halt()
            if self._db:
                self._db.record_halt(reason)
            logger.error("RISK_CHECK HARD breach: flattened + halted (%s)", reason)
        return action.value

    def shutdown(self) -> None:
        # Cancel-on-shutdown (CLAUDE.md). Best-effort flatten of working orders:
        # a cancel failure is logged, never swallowed silently, and never masks
        # the real shutdown reason.
        try:
            self._b.cancel_all()
            logger.info("shutdown: cancel_all completed")
        except Exception as e:
            logger.error("shutdown: cancel_all failed: %s", e)
            raise


def main() -> int:  # pragma: no cover — live entrypoint, covered by manual run
    import os
    import sys
    import time
    from autotrader.strategies.threshold import StrategyParams

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    cfg = load_risk_config()
    logger.info("TRADING_ENV=%s (paper-only v1)", cfg.trading_env)
    if cfg.trading_env != "PAPER":
        logger.error("v1 is paper-only; refusing to start with TRADING_ENV=%s", cfg.trading_env)
        return 2

    # Readiness gate: reuse the dashboard's exponential-backoff is_opend_ready().
    sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "dashboard"))
    from opend_ready import is_opend_ready, OpenDNotReady  # type: ignore
    try:
        is_opend_ready(timeout=float(os.getenv("OPEND_READY_TIMEOUT", "30")))
    except OpenDNotReady as e:
        logger.error("halting: %s", e)
        return 1

    from autotrader.moomoo_broker import MoomooBroker
    symbol = next(iter(cfg.allowed_symbols), "US.AAPL")
    strat = ThresholdStrategy(StrategyParams(
        symbol=symbol, entry_price=float(os.getenv("ENTRY_PRICE", "0")),
        stop_loss_pct=0.05, take_profit_pct=0.10, confidence=0.7))
    audit = os.path.join(os.path.expanduser("~"), ".futu_trade_audit.jsonl")
    broker = MoomooBroker()
    broker.connect()
    db_path = os.path.expanduser(os.getenv("AUTOTRADER_DB_PATH", "~/.autotrader.db"))
    from autotrader.db import DB  # lazy import: keeps tests that skip main() SDK-free
    db = DB(db_path)
    logger.info("DB projection at %s", db_path)
    from autotrader.clock import Clock
    from autotrader.lifecycle import EntryGate, ground_truth_sync
    from autotrader.scheduler import LifecycleScheduler
    from autotrader.watchdog import Watchdog
    from autotrader.runner import SessionRunner

    gate = EntryGate(enabled=False)  # entries open at 09:45 via the scheduler
    engine = TradeEngine(broker, strat, cfg, order_qty=int(os.getenv("ORDER_QTY", "1")),
                         audit_path=audit, db=db, entry_gate=gate)
    watchdog = Watchdog(
        health_check=broker.heartbeat,
        reconcile=lambda: ground_truth_sync(broker, db),
        sleep=time.sleep,
    )
    inbox = None
    inbox_dir = os.getenv("AUTOTRADER_SIGNAL_INBOX")
    if inbox_dir:
        from autotrader.signals.inbox import SignalInbox
        inbox = SignalInbox(os.path.expanduser(inbox_dir),
                            on_targets=db.upsert_target_weights)
        logger.info("external-signal inbox at %s", inbox_dir)
    reporter = None
    slack_url = os.getenv("AUTOTRADER_SLACK_WEBHOOK_URL")
    if slack_url:
        from autotrader.reporting.eod_reporter import EODReporter
        reporter = EODReporter(db=db, webhook_url=slack_url,
                               trading_env=cfg.trading_env)
        logger.info("EOD Slack reporter enabled")
    else:
        # Loud about the disabled path: the 16:30 EOD_REPORT job is otherwise a
        # silent no-op (runner guards on reporter is not None). Usually means
        # config/secure.config was not sourced into the trader's env (RUNBOOK §4).
        logger.info("EOD Slack reporter disabled (AUTOTRADER_SLACK_WEBHOOK_URL unset)")
    runner = SessionRunner(
        engine=engine, broker=broker, db=db, gate=gate,
        scheduler=LifecycleScheduler(), watchdog=watchdog, clock=Clock(),
        sleep=time.sleep,
        loop_interval=float(os.getenv("AUTOTRADER_LOOP_INTERVAL", "5")),
        signal_inbox=inbox,
        reporter=reporter,
    )

    stopped = {"flag": False}

    def _stop() -> bool:
        return stopped["flag"]

    try:
        runner.run(stop=_stop)        # runs until KeyboardInterrupt
    except KeyboardInterrupt:
        logger.info("interrupt received — shutting down")
    finally:
        # Always disconnect, even if cancel-on-shutdown raises — a failed
        # cancel_all must not leak the OpenD connection.
        try:
            engine.shutdown()
        except Exception as e:
            logger.error("shutdown error (working orders may remain): %s", e)
        broker.close()
        db.close()
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
