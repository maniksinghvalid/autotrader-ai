"""Orchestration. One deterministic tick: snapshot -> quote -> strategy ->
confidence filter -> risk core -> router. Shutdown cancels all open orders
before disconnect (CLAUDE.md). The engine takes any Broker, so it is testable
against SimBroker with no OpenD. The __main__ path wires a live MoomooBroker
behind is_opend_ready()."""
from __future__ import annotations

import logging
from dataclasses import dataclass

from autotrader.broker import Broker
from autotrader.config import RiskConfig, load_risk_config
from autotrader.domain import OrderRequest, OrderState
from autotrader.risk_core import evaluate
from autotrader.router import OrderRouter
from autotrader.strategies.threshold import ThresholdStrategy

logger = logging.getLogger("autotrader.engine")


@dataclass(frozen=True)
class TickResult:
    action: str
    detail: str = ""


class TradeEngine:
    def __init__(self, broker: Broker, strategy: ThresholdStrategy, cfg: RiskConfig,
                 order_qty: int, audit_path: str):
        self._b = broker
        self._strat = strategy
        self._cfg = cfg
        self._qty = order_qty
        self._router = OrderRouter(broker, audit_path=audit_path)
        self._signal_seq = 0

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

        self._signal_seq += 1
        cid = OrderRouter.make_client_order_id(
            signal.symbol, signal.direction, self._qty, f"sig-{self._signal_seq}")
        req = OrderRequest(symbol=signal.symbol, side=signal.direction, qty=self._qty,
                           order_type="MARKET", limit_price=None, client_order_id=cid)

        decision = evaluate(req, snap, self._cfg, ref_price=price)
        if not decision.approved:
            logger.warning("risk core rejected: %s", decision.reason)
            return TickResult("REJECTED_BY_RISK", decision.reason)

        ack = self._router.submit(req)
        if ack.state is OrderState.UNKNOWN:
            return TickResult("ORDER_UNKNOWN", ack.client_order_id)
        if ack.state is OrderState.REJECTED:
            return TickResult("ORDER_REJECTED", ack.client_order_id)
        return TickResult("ORDER_PLACED", str(ack.broker_order_id))

    def shutdown(self) -> None:
        # Cancel-on-shutdown (CLAUDE.md). Best-effort flatten of working orders.
        try:
            self._b.cancel_all()
        finally:
            logger.info("shutdown: cancel_all issued")


def main() -> int:  # pragma: no cover — live entrypoint, covered by manual run
    import os
    import sys
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
    engine = TradeEngine(broker, strat, cfg, order_qty=int(os.getenv("ORDER_QTY", "1")),
                         audit_path=audit)
    try:
        result = engine.tick()  # v1: single deterministic tick; loop added in Phase 2
        logger.info("tick result: %s %s", result.action, result.detail)
    finally:
        engine.shutdown()
        broker.close()
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
