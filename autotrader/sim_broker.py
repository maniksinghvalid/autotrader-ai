"""In-memory Broker for deterministic unit tests. No OpenD, no SDK, no clock
dependence (fill ids/timestamps are counters)."""
from __future__ import annotations

from typing import Dict, List, Optional

from autotrader.broker import Broker
from autotrader.domain import (
    AccountSnapshot, Fill, OrderAck, OrderRequest, OrderState, Position,
)


class SimBroker(Broker):
    def __init__(self, quotes: Dict[str, float], cash: float = 10000.0,
                 auto_fill: bool = True):
        self._quotes = dict(quotes)
        self._cash = cash
        self._positions: Dict[str, Position] = {}
        self._open: Dict[str, OrderAck] = {}
        self._fills: List[Fill] = []
        self._acks_by_cid: Dict[str, OrderAck] = {}
        self._auto_fill = auto_fill
        self._seq = 0

    def connect(self) -> None:
        return None

    def is_ready(self) -> bool:
        return True

    def get_quote(self, symbol: str) -> Optional[float]:
        return self._quotes.get(symbol)

    def place_order(self, req: OrderRequest) -> OrderAck:
        if req.client_order_id in self._acks_by_cid:  # idempotency (R8)
            return self._acks_by_cid[req.client_order_id]
        self._seq += 1
        boid = f"sim-{self._seq}"
        price = req.limit_price or self._quotes.get(req.symbol, 0.0)
        # A TRAILING_STOP is a broker-RESTING protective order: it never fills
        # immediately, regardless of auto_fill (it waits for the trail to trigger).
        rests = req.order_type == "TRAILING_STOP" or not self._auto_fill
        if not rests:
            signed = req.qty if req.side == "BUY" else -req.qty
            self._cash -= signed * price
            prev = self._positions.get(req.symbol)
            new_qty = (prev.qty if prev else 0) + signed
            self._positions[req.symbol] = Position(req.symbol, new_qty, price)
            self._fills.append(Fill(fill_id=f"fill-{self._seq}", symbol=req.symbol,
                                    side=req.side, qty=req.qty, price=price,
                                    ts=f"t{self._seq}"))
            ack = OrderAck(req.client_order_id, boid, OrderState.FILLED, {})
        else:
            ack = OrderAck(req.client_order_id, boid, OrderState.SUBMITTED, {})
            self._open[boid] = ack
        self._acks_by_cid[req.client_order_id] = ack
        return ack

    def cancel_order(self, broker_order_id: str) -> None:
        self._open.pop(broker_order_id, None)

    def cancel_all(self) -> None:
        self._open.clear()

    def get_account(self) -> AccountSnapshot:
        positions = tuple(self._positions.values())
        return AccountSnapshot(cash=self._cash, total_assets=self._cash,
                               day_pnl=0.0, stale=False, positions=positions)

    def get_open_orders(self) -> List[OrderAck]:
        return list(self._open.values())

    def reconcile_fills(self, since: Optional[str]) -> List[Fill]:
        return list(self._fills)

    def close(self) -> None:
        return None
