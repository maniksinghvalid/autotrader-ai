"""In-memory Broker for deterministic unit tests. No OpenD, no SDK, no clock
dependence (fill ids/timestamps are counters)."""
from __future__ import annotations

from typing import Dict, List, Optional

from autotrader.broker import Broker
from autotrader.domain import (
    AccountSnapshot, Fill, OptionRight, OrderAck, OrderRequest, OrderState,
    Position,
)
from autotrader.options.chain import OptionQuote


class SimBroker(Broker):
    def __init__(self, quotes: Dict[str, float], cash: float = 10000.0,
                 auto_fill: bool = True, option_chains=None,
                 spread_bps: float = 0.0, slippage_bps: float = 0.0,
                 recent_highs: Optional[Dict[str, float]] = None,
                 fill_latency_ticks: int = 0, cancel_latency_ticks: int = 0):
        self._quotes = dict(quotes)
        self._cash = cash
        self._positions: Dict[str, Position] = {}
        self._open: Dict[str, OrderAck] = {}
        self._fills: List[Fill] = []
        self._acks_by_cid: Dict[str, OrderAck] = {}
        self._auto_fill = auto_fill
        self._seq = 0
        # Paper spread/slippage model (basis points of the reference quote). Both
        # default to 0 => bid==ask==ref and MARKET fills at ref (today's behavior).
        self._spread_bps = spread_bps
        self._slippage_bps = slippage_bps
        # keyed by (underlying.upper(), right.upper()) -> List[OptionQuote]
        self._chains = {(u.upper(), r.upper()): list(v)
                        for (u, r), v in (option_chains or {}).items()}
        # Failure injection for tests of the None-vs-empty broker contract.
        self.fail_open_orders = False
        self.fail_reconcile_fills = False
        self._recent_highs = dict(recent_highs or {})
        # V1 async rig: 0 = synchronous (today's behavior). >0 = orders ack
        # SUBMITTED and fill/cancel only after N tick_market() calls; within a
        # tick, fills mature BEFORE cancels (the live cancel-race, deterministic).
        self._fill_latency = int(fill_latency_ticks)
        self._cancel_latency = int(cancel_latency_ticks)
        self._pending_fills: Dict[str, dict] = {}    # boid -> {req, price, left}
        self._pending_cancels: Dict[str, int] = {}   # boid -> ticks left

    def connect(self) -> None:
        return None

    def is_ready(self) -> bool:
        return True

    def get_quote(self, symbol: str) -> Optional[float]:
        return self._quotes.get(symbol)

    def get_touch(self, symbol: str):
        if symbol not in self._quotes:
            return None
        return self._touch(symbol)

    def recent_high(self, symbol: str, lookback: int) -> Optional[float]:
        return self._recent_highs.get(symbol)

    def _touch(self, symbol: str):
        """Simulated (bid, ask) around the stored reference. Half-spread each side."""
        ref = self._quotes.get(symbol, 0.0)
        half = ref * (self._spread_bps / 1e4)
        return ref - half, ref + half

    def _market_fill_price(self, symbol: str, side: str) -> float:
        bid, ask = self._touch(symbol)
        slip = self._slippage_bps / 1e4
        return ask * (1 + slip) if side == "BUY" else bid * (1 - slip)

    def _limit_is_marketable(self, symbol: str, side: str, limit_price: float) -> bool:
        bid, ask = self._touch(symbol)
        return limit_price >= ask if side == "BUY" else limit_price <= bid

    def get_option_chain(self, underlying: str, right: OptionRight,
                         dte_min: int = 0, dte_max: int = 100000) -> List[OptionQuote]:
        # Window args accepted for interface parity; the precise DTE filter runs
        # in select_contract over the canned chain.
        return list(self._chains.get((underlying.upper(), right.upper()), []))

    def place_order(self, req: OrderRequest) -> OrderAck:
        if req.client_order_id in self._acks_by_cid:  # idempotency (R8)
            return self._acks_by_cid[req.client_order_id]
        self._seq += 1
        boid = f"sim-{self._seq}"
        # A TRAILING_STOP is broker-RESTING; a non-marketable LIMIT rests too.
        if req.order_type == "TRAILING_STOP" or not self._auto_fill:
            rests = True
            price = 0.0
        elif req.order_type == "LIMIT":
            if self._limit_is_marketable(req.symbol, req.side, req.limit_price):
                rests = False
                price = req.limit_price          # marketable limit fills at its price
            else:
                rests = True
                price = 0.0
        else:  # MARKET
            rests = False
            price = self._market_fill_price(req.symbol, req.side)
        if not rests and self._fill_latency > 0:
            ack = OrderAck(req.client_order_id, boid, OrderState.SUBMITTED, {})
            self._open[boid] = ack
            self._pending_fills[boid] = {"req": req, "price": price,
                                         "left": self._fill_latency}
            self._acks_by_cid[req.client_order_id] = ack
            return ack
        if not rests:
            self._apply_fill(req, price, self._seq)
            ack = OrderAck(req.client_order_id, boid, OrderState.FILLED, {})
        else:
            ack = OrderAck(req.client_order_id, boid, OrderState.SUBMITTED, {})
            self._open[boid] = ack
        self._acks_by_cid[req.client_order_id] = ack
        return ack

    def cancel_order(self, broker_order_id: str) -> None:
        if broker_order_id not in self._open:
            return
        if self._cancel_latency > 0:
            self._pending_cancels[broker_order_id] = self._cancel_latency
            return
        self._open.pop(broker_order_id, None)
        self._pending_fills.pop(broker_order_id, None)

    def tick_market(self) -> None:
        """Advance one sim tick: mature pending fills FIRST, then pending
        cancels — a fill and cancel due the same tick resolves as a fill
        (the live race a cancel-then-resubmit path must survive)."""
        for boid in list(self._pending_fills):
            entry = self._pending_fills[boid]
            entry["left"] -= 1
            if entry["left"] > 0:
                continue
            req, price = entry["req"], entry["price"]
            self._seq += 1
            self._apply_fill(req, price, self._seq)
            del self._pending_fills[boid]
            self._open.pop(boid, None)
            self._pending_cancels.pop(boid, None)   # fill won the race
        for boid in list(self._pending_cancels):
            self._pending_cancels[boid] -= 1
            if self._pending_cancels[boid] <= 0:
                del self._pending_cancels[boid]
                self._open.pop(boid, None)
                self._pending_fills.pop(boid, None)

    def _apply_fill(self, req: OrderRequest, price: float, seq: int) -> None:
        """Apply a fill: update cash, positions, and record fill.
        seq must be pre-incremented by the caller."""
        signed = req.qty if req.side == "BUY" else -req.qty
        self._cash -= signed * price
        prev = self._positions.get(req.symbol)
        new_qty = (prev.qty if prev else 0) + signed
        self._positions[req.symbol] = Position(req.symbol, new_qty, price)
        fill_id = f"fill-{seq}"
        ts = f"t{seq}"
        self._fills.append(Fill(fill_id=fill_id, symbol=req.symbol,
                                side=req.side, qty=req.qty, price=price, ts=ts))

    def cancel_all(self) -> None:
        self._open.clear()
        self._pending_fills.clear()
        self._pending_cancels.clear()

    def get_account(self) -> AccountSnapshot:
        positions = tuple(self._positions.values())
        return AccountSnapshot(cash=self._cash, total_assets=self._cash,
                               day_pnl=0.0, stale=False, positions_loaded=True,
                               unrealized_pnl=0.0, positions=positions)

    def get_open_orders(self) -> Optional[List[OrderAck]]:
        if self.fail_open_orders:
            return None   # simulate a failed/rate-limited query (unknown book)
        return list(self._open.values())

    def reconcile_fills(self, since: Optional[str]) -> Optional[List[Fill]]:
        if self.fail_reconcile_fills:
            return None
        return list(self._fills)

    def close(self) -> None:
        return None
