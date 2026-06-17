"""MoomooBroker — the ONLY app module that imports the vendored common.py / SDK.
Confines all Moomoo-isms: US.AAPL codes, (ret_code, data), refresh_cache=True,
OrderStatus mapping, TrdEnv.SIMULATE. Never sends an SDK trade-unlock — that is
a manual OpenD GUI action (CLAUDE.md hard rule). Reuses common.py factories
rather than constructing a trade context directly, so env checks aren't bypassed."""
from __future__ import annotations

import logging
import os
import sys
from typing import List, Optional

from autotrader.broker import Broker
from autotrader.domain import (
    AccountSnapshot, BrokerError, BrokerErrorKind, Fill, OrderAck, OrderRequest,
    OrderState, Position,
)
from autotrader.rate_limiter import RateLimiter

logger = logging.getLogger("autotrader.broker")

# Resolve the vendored scripts dir and import common.py (triggers its env checks).
_REPO_ROOT = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
_MOOMOO_SCRIPTS = os.path.join(_REPO_ROOT, "skills", "moomooapi", "scripts")


def _load_common():
    if _MOOMOO_SCRIPTS not in sys.path:
        sys.path.insert(0, _MOOMOO_SCRIPTS)
    import common  # noqa: WPS433 — intentional late import; runs OpenD/SDK checks
    return common


# Moomoo OrderStatus name -> neutral OrderState.
_STATUS_MAP = {
    "SUBMITTED": OrderState.SUBMITTED, "SUBMITTING": OrderState.PENDING,
    "WAITING_SUBMIT": OrderState.PENDING, "FILLED_ALL": OrderState.FILLED,
    "FILLED_PART": OrderState.PARTIAL, "CANCELLED_ALL": OrderState.CANCELLED,
    "CANCELLED_PART": OrderState.CANCELLED, "FAILED": OrderState.REJECTED,
    "DISABLED": OrderState.REJECTED, "DELETED": OrderState.CANCELLED,
}


class MoomooBroker(Broker):
    def __init__(self, acc_id: Optional[int] = None):
        self._c = _load_common()
        self._acc_id = acc_id if acc_id is not None else self._c.get_default_acc_id()
        self._trade = None
        self._quote = None
        self._order_rl = RateLimiter(capacity=15.0, refill_rate=0.5)    # 15 orders / 30 s
        self._refresh_rl = RateLimiter(capacity=10.0, refill_rate=10 / 30)  # 10 refresh / 30 s

    # --- lifecycle -------------------------------------------------------
    def connect(self) -> None:
        self._trade = self._c.create_trade_context("US")
        self._quote = self._c.create_quote_context()

    def is_ready(self) -> bool:
        return self._trade is not None and self._quote is not None

    def heartbeat(self) -> bool:  # pragma: no cover — live OpenD path
        if self._quote is None:
            return False
        try:
            ret, _ = self._quote.get_global_state()
        except Exception:
            return False
        return self._ok(ret)

    def close(self) -> None:
        self._c.safe_close(self._trade)
        self._c.safe_close(self._quote)
        self._trade = self._quote = None

    # --- helpers ---------------------------------------------------------
    def _env(self):
        return self._c.get_default_trd_env()

    def _ok(self, ret) -> bool:
        return ret == self._c.RET_OK

    # --- market data -----------------------------------------------------
    def get_quote(self, symbol: str) -> Optional[float]:
        ret, data = self._quote.get_market_snapshot([symbol])
        if not self._ok(ret) or self._c.is_empty(data):
            return None
        return self._c.safe_float(self._c.safe_get(data.iloc[0], "last_price", default=0)) or None

    def get_option_chain(self, underlying: str, right):  # pragma: no cover — live OpenD
        """Live option chain for `underlying` (e.g. US.AAPL) and `right`
        (CALL/PUT), returned as options.chain.OptionQuote rows.

        All SDK/options imports are confined here — the module stays SDK-free at
        import time (matches the existing lazy-import convention in this file).

        Field names validated during the live paper smoke test; safe_get tolerates
        the multiple candidate keys listed below in case SDK version changes them.
        Vendored field names from get_option_chain.py: code, strike_price,
        strike_time, last_price. Snapshot greeks: option_delta, option_strike_price,
        option_expiry_date (alternatives kept as fallbacks)."""
        from datetime import date as _date, datetime, timedelta
        from autotrader.options.chain import OptionQuote
        from moomoo import OptionType  # confined import

        opt_type = OptionType.CALL if str(right).upper() == "CALL" else OptionType.PUT
        # start/end are optional per vendored script; pass a 90-day window so we
        # get a useful range without flooding the response with far-dated expiries.
        today = _date.today()
        start_str = today.strftime("%Y-%m-%d")
        end_str = (today + timedelta(days=90)).strftime("%Y-%m-%d")

        ret, chain = self._quote.get_option_chain(
            underlying, option_type=opt_type, start=start_str, end=end_str)
        if not self._ok(ret) or self._c.is_empty(chain):
            return []

        # chain df has at least: code, name, option_type, strike_price,
        # strike_time, last_price (per vendored get_option_chain.py columns).
        codes = [str(self._c.safe_get(chain.iloc[i], "code", default=""))
                 for i in range(len(chain))]
        codes = [c for c in codes if c]
        if not codes:
            return []

        sret, snap = self._quote.get_market_snapshot(codes)
        if not self._ok(sret) or self._c.is_empty(snap):
            return []

        out = []
        for i in range(len(snap)):
            row = snap.iloc[i]
            code = str(self._c.safe_get(row, "code", default=""))
            # strike: snapshot may use option_strike_price or strike_price
            strike = self._c.safe_float(self._c.safe_get(
                row, "option_strike_price", "strike_price", "strike", default=0))
            # expiry: snapshot may use option_expiry_date; chain used strike_time
            exp = str(self._c.safe_get(
                row, "option_expiry_date", "strike_time", "expiry_date", default=""))
            try:
                expiry = datetime.strptime(exp[:10], "%Y-%m-%d").date()
            except (ValueError, TypeError):
                continue
            bid = self._c.safe_float(self._c.safe_get(row, "bid_price", "bid", default=0))
            ask = self._c.safe_float(self._c.safe_get(row, "ask_price", "ask", default=0))
            mid = (bid + ask) / 2 if (bid and ask) else self._c.safe_float(
                self._c.safe_get(row, "last_price", "cur_price", default=0))
            delta = self._c.safe_float(self._c.safe_get(
                row, "option_delta", "delta", default=0))
            if strike <= 0 or mid <= 0:
                continue
            out.append(OptionQuote(code=code, underlying=underlying, expiry=expiry,
                                   strike=strike, right=str(right).upper(),
                                   delta=delta, premium=mid))
        return out

    # --- orders ----------------------------------------------------------
    def place_order(self, req: OrderRequest) -> OrderAck:
        if not self._order_rl.acquire(timeout=60.0):
            raise BrokerError(BrokerErrorKind.RATE_LIMIT,
                               "order rate limit: timed out waiting for order token")
        side = self._c.TrdSide.BUY if req.side == "BUY" else self._c.TrdSide.SELL
        kwargs = dict(qty=int(req.qty), code=req.symbol, trd_side=side,
                      trd_env=self._env(), acc_id=self._acc_id,
                      remark=req.client_order_id[:64])  # idempotency key in remark (R8)
        if req.order_type == "MARKET":
            kwargs.update(price=0.0, order_type=self._c.OrderType.MARKET)
        elif req.order_type == "TRAILING_STOP":  # pragma: no cover — live OpenD path
            from moomoo import TrailType  # confined to this adapter
            kwargs.update(price=0.0, order_type=self._c.OrderType.TRAILING_STOP,
                          trail_type=TrailType.RATIO, trail_value=float(req.trail_percent))
        else:  # LIMIT
            kwargs.update(price=float(req.limit_price), order_type=self._c.OrderType.NORMAL)
        try:
            ret, data = self._trade.place_order(**kwargs)
        except Exception as e:  # socket drop / timeout -> UNKNOWN, never success
            return OrderAck(req.client_order_id, None, OrderState.UNKNOWN, {"error": str(e)})
        if not self._ok(ret):
            return OrderAck(req.client_order_id, None, OrderState.REJECTED, {"error": str(data)})
        row = data.iloc[0]
        boid = str(self._c.safe_get(row, "order_id", "orderID", default=""))
        return OrderAck(req.client_order_id, boid, OrderState.SUBMITTED, {})

    def cancel_order(self, broker_order_id: str) -> None:
        from moomoo import ModifyOrderOp  # confined to this adapter
        logger.info("cancel_order: %s", broker_order_id)
        ret, data = self._trade.modify_order(
            modify_order_op=ModifyOrderOp.CANCEL, order_id=broker_order_id,
            qty=0, price=0, trd_env=self._env(), acc_id=self._acc_id)
        if not self._ok(ret):
            raise BrokerError(BrokerErrorKind.UNKNOWN, f"cancel failed: {data}")

    def cancel_all(self) -> None:
        orders = self.get_open_orders()
        logger.info("cancel_all: %d open order(s) to cancel", len(orders))
        for ack in orders:
            if ack.broker_order_id:
                try:
                    self.cancel_order(ack.broker_order_id)
                except BrokerError:
                    pass  # best-effort flatten on shutdown; logged by cancel_order

    def get_open_orders(self) -> List[OrderAck]:
        if not self._refresh_rl.acquire(timeout=60.0):
            return []  # best-effort on shutdown/monitoring path
        ret, data = self._trade.order_list_query(
            trd_env=self._env(), acc_id=self._acc_id, refresh_cache=True)
        if not self._ok(ret) or self._c.is_empty(data):
            return []
        out: List[OrderAck] = []
        for i in range(len(data)):
            row = data.iloc[i]
            status = self._c.format_enum(self._c.safe_get(row, "order_status", default="")).upper()
            state = _STATUS_MAP.get(status, OrderState.UNKNOWN)
            if state in (OrderState.PENDING, OrderState.SUBMITTED, OrderState.PARTIAL):
                out.append(OrderAck(
                    str(self._c.safe_get(row, "remark", default="")),
                    str(self._c.safe_get(row, "order_id", default="")), state, {}))
        return out

    # --- account / positions / fills ------------------------------------
    def get_account(self) -> AccountSnapshot:
        if not self._refresh_rl.acquire(timeout=60.0):
            raise BrokerError(BrokerErrorKind.RATE_LIMIT,
                               "refresh rate limit: timed out waiting for account token")
        ret, acc = self._trade.accinfo_query(
            trd_env=self._env(), acc_id=self._acc_id, refresh_cache=True)
        if not self._ok(ret):
            raise BrokerError(BrokerErrorKind.UNKNOWN, f"accinfo failed: {acc}")
        cash = self._c.safe_float(self._c.safe_get(acc.iloc[0], "cash", "avl_withdrawal_cash", default=0))
        total = self._c.safe_float(self._c.safe_get(acc.iloc[0], "total_assets", default=0))
        pnl = self._c.safe_float(self._c.safe_get(acc.iloc[0], "realized_pl", "today_pnl_value", default=0))
        positions = self._positions()
        if positions is None:
            # Position query FAILED — never present a falsely-flat snapshot the
            # risk core would trust. Mark stale so it refuses to trade (E1/R7).
            return AccountSnapshot(cash=cash, total_assets=total, day_pnl=pnl,
                                   stale=True, positions=())
        # A genuinely empty account with zero assets is also treated as stale.
        stale = (total == 0 and not positions)
        return AccountSnapshot(cash=cash, total_assets=total, day_pnl=pnl,
                               stale=stale, positions=tuple(positions))

    def _positions(self) -> Optional[List[Position]]:
        """Return current positions, or None if the position query FAILED.

        None (query error) is distinct from [] (genuinely flat): the caller
        turns a None into a stale snapshot rather than a falsely-flat one.
        """
        if not self._refresh_rl.acquire(timeout=60.0):
            return None  # treat rate-limit failure as a failed query -> stale snapshot
        ret, data = self._trade.position_list_query(
            trd_env=self._env(), acc_id=self._acc_id, refresh_cache=True)
        if not self._ok(ret):
            return None  # query failed — do NOT report flat
        if self._c.is_empty(data):
            return []    # genuinely no positions
        out: List[Position] = []
        for i in range(len(data)):
            row = data.iloc[i]
            out.append(Position(
                symbol=str(self._c.safe_get(row, "code", default="")),
                qty=self._c.safe_int(self._c.safe_get(row, "qty", default=0)),
                avg_price=self._c.safe_float(self._c.safe_get(row, "cost_price", "nominal_price", default=0)),
            ))
        return out

    def get_open_orders_count(self) -> int:  # convenience for logs
        return len(self.get_open_orders())

    def reconcile_fills(self, since: Optional[str]) -> List[Fill]:
        if not self._refresh_rl.acquire(timeout=60.0):
            return []
        kwargs = dict(trd_env=self._env(), acc_id=self._acc_id, refresh_cache=True)
        if since:
            kwargs["begin_time"] = since
        ret, data = self._trade.deal_list_query(**kwargs)
        if not self._ok(ret) or self._c.is_empty(data):
            return []
        out: List[Fill] = []
        for i in range(len(data)):
            row = data.iloc[i]
            side_raw = self._c.format_enum(self._c.safe_get(row, "trd_side", default="BUY")).upper()
            out.append(Fill(
                fill_id=str(self._c.safe_get(row, "deal_id", default="")),
                symbol=str(self._c.safe_get(row, "code", default="")),
                side="BUY" if side_raw == "BUY" else "SELL",
                qty=self._c.safe_float(self._c.safe_get(row, "qty", default=0)),
                price=self._c.safe_float(self._c.safe_get(row, "price", default=0)),
                ts=str(self._c.safe_get(row, "create_time", default="")),
            ))
        return out
