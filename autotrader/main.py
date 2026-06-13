"""Orchestration. One deterministic tick: snapshot -> quote -> strategy ->
confidence filter -> risk core -> router. Shutdown cancels all open orders
before disconnect (CLAUDE.md). The engine takes any Broker, so it is testable
against SimBroker with no OpenD. The __main__ path wires a live MoomooBroker
behind is_opend_ready()."""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Optional

from autotrader.broker import Broker
from autotrader.config import RiskConfig, load_risk_config
from autotrader.domain import OrderRequest, OrderState
from autotrader.risk_core import evaluate
from autotrader.router import OrderRouter
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
                 entry_gate: "Optional[EntryGate]" = None):
        self._b = broker
        self._strat = strategy
        self._cfg = cfg
        self._qty = order_qty
        self._router = OrderRouter(broker, audit_path=audit_path)
        self._signal_seq = 0
        self._db = db
        self._gate = entry_gate

    def tick(self) -> TickResult:
        snap = self._b.get_account()
        symbol = self._strat.p.symbol
        price = self._b.get_quote(symbol)
        if price is None:
            return TickResult("NO_QUOTE", symbol)

        pos = next((p for p in snap.positions if p.symbol == symbol), None)
        signal = self._strat.evaluate(price=price, position=pos)
        if signal is None:
            return TickResult("NO_SIGNAL")

        if signal.confidence < self._cfg.min_confidence:
            return TickResult("DROPPED_LOW_CONFIDENCE", f"{signal.confidence}")

        # Entry-window gate: block NEW entries (BUY) when closed; exits (SELL)
        # are never gated — you must always be able to flatten.
        if (signal.direction == "BUY" and self._gate is not None
                and not self._gate.entries_enabled):
            return TickResult("ENTRY_CLOSED", signal.symbol)

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

        # SELL exits must liquidate the full position; BUY uses the configured order_qty.
        sell_qty = pos.qty if (signal.direction == "SELL" and pos is not None) else self._qty
        cid = OrderRouter.make_client_order_id(signal.symbol, signal.direction, sell_qty, signal_id)
        req = OrderRequest(symbol=signal.symbol, side=signal.direction, qty=sell_qty,
                           order_type="MARKET", limit_price=None, client_order_id=cid)

        decision = evaluate(req, snap, self._cfg, ref_price=price)
        if not decision.approved:
            logger.warning("risk core rejected: %s", decision.reason)
            return TickResult("REJECTED_BY_RISK", decision.reason)

        ack = self._router.submit(req)
        if self._db:
            self._db.record_trade(
                client_order_id=ack.client_order_id,
                symbol=req.symbol,
                side=req.side,
                qty=req.qty,
                order_type=req.order_type,
                limit_price=req.limit_price,
                broker_order_id=ack.broker_order_id,
                state=ack.state.value,
            )
        if ack.state is OrderState.UNKNOWN:
            return TickResult("ORDER_UNKNOWN", ack.client_order_id)
        if ack.state is OrderState.REJECTED:
            return TickResult("ORDER_REJECTED", ack.client_order_id)
        return TickResult("ORDER_PLACED", str(ack.broker_order_id))

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
    runner = SessionRunner(
        engine=engine, broker=broker, db=db, gate=gate,
        scheduler=LifecycleScheduler(), watchdog=watchdog, clock=Clock(),
        sleep=time.sleep,
        loop_interval=float(os.getenv("AUTOTRADER_LOOP_INTERVAL", "5")),
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
