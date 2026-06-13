"""Order router. The ONLY path to broker.place_order. Invariants:
- the audit line is written BEFORE the order is placed (E18/R16); an audit
  write failure is a hard error that halts the order.
- a deterministic client_order_id makes retries idempotent (R8).
- orders are serialized per symbol (E9).
No clock/random in the hot path beyond an ISO timestamp for the audit line."""
from __future__ import annotations

import collections
import hashlib
import json
import threading
from datetime import datetime, timezone
from typing import Dict

from autotrader.broker import Broker
from autotrader.domain import OrderAck, OrderRequest, OrderState


class OrderRouter:
    def __init__(self, broker: Broker, audit_path: str):
        self._broker = broker
        self._audit_path = audit_path
        self._seen: Dict[str, OrderAck] = {}
        self._locks: Dict[str, threading.Lock] = collections.defaultdict(threading.Lock)
        self._global = threading.Lock()

    @staticmethod
    def make_client_order_id(symbol: str, side: str, qty: int, signal_id: str) -> str:
        raw = f"{symbol}|{side}|{qty}|{signal_id}"
        return "at-" + hashlib.sha1(raw.encode()).hexdigest()[:16]

    def _audit(self, action: str, payload: dict) -> None:
        """Append one JSONL line. Raises RuntimeError on failure — caller must NOT proceed."""
        entry = {"action": action, "ts": datetime.now(timezone.utc).isoformat(), **payload}
        try:
            with open(self._audit_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")
                f.flush()
        except OSError as exc:
            raise RuntimeError(
                f"Audit write failed for action={action!r} path={self._audit_path!r}: {exc}"
            ) from exc

    def submit(self, req: OrderRequest) -> OrderAck:
        with self._global:
            if req.client_order_id in self._seen:
                return self._seen[req.client_order_id]
            lock = self._locks[req.symbol]
        with lock:
            if req.client_order_id in self._seen:  # re-check under symbol lock
                return self._seen[req.client_order_id]
            # AUDIT FIRST — if this raises, no order is placed (E18).
            self._audit("intent", {
                "client_order_id": req.client_order_id, "symbol": req.symbol,
                "side": req.side, "qty": req.qty, "order_type": req.order_type,
                "limit_price": req.limit_price, "trail_percent": req.trail_percent,
            })
            try:
                ack = self._broker.place_order(req)
            except Exception as e:  # ACK timeout / socket drop -> UNKNOWN, never success
                ack = OrderAck(req.client_order_id, None, OrderState.UNKNOWN, {"error": str(e)})
                self._audit("ack", {"client_order_id": req.client_order_id,
                                    "state": ack.state.value, "error": str(e)})
                self._seen[req.client_order_id] = ack
                return ack
            self._audit("ack", {"client_order_id": req.client_order_id,
                                "broker_order_id": ack.broker_order_id,
                                "state": ack.state.value})
            self._seen[req.client_order_id] = ack
            return ack
