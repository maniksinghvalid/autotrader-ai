"""MoomooBroker — the ONLY app module that imports the vendored common.py / SDK.
Confines all Moomoo-isms: US.AAPL codes, (ret_code, data), refresh_cache=True,
OrderStatus mapping, TrdEnv.SIMULATE. Never sends an SDK trade-unlock — that is
a manual OpenD GUI action (CLAUDE.md hard rule). Reuses common.py factories
rather than constructing a trade context directly, so env checks aren't bypassed."""
from __future__ import annotations

import logging
import math
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


def _chain_windows(expiries, today, dte_min, dte_max, max_span=30):
    """Group expiries that fall within [today+dte_min, today+dte_max] into
    <=max_span-day (start, end) windows, so each get_option_chain call stays
    under the 30-day span cap while covering only real expiries (not blank
    calendar). Pure: no SDK, importable without OpenD."""
    qualifying = sorted(e for e in expiries
                        if dte_min <= (e - today).days <= dte_max)
    windows = []
    i = 0
    while i < len(qualifying):
        start = qualifying[i]
        j = i
        while j + 1 < len(qualifying) and (qualifying[j + 1] - start).days <= max_span:
            j += 1
        windows.append((start, qualifying[j]))
        i = j + 1
    return windows


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

    def get_touch(self, symbol: str):
        """(bid, ask) from a market snapshot, or None (quote failure / no book).
        Quote-context call — does not consume trade refresh tokens."""
        ret, data = self._quote.get_market_snapshot([symbol])
        if not self._ok(ret) or self._c.is_empty(data):
            return None
        row = data.iloc[0]
        bid = self._c.safe_float(self._c.safe_get(row, "bid_price", "bid", default=0))
        ask = self._c.safe_float(self._c.safe_get(row, "ask_price", "ask", default=0))
        if bid <= 0 or ask <= 0:
            return None
        return (bid, ask)

    def recent_high(self, symbol: str, lookback: int):
        """Highest daily high over the last `lookback` COMPLETED trading days
        (today's forming bar excluded), or None on data failure / insufficient
        history. Quote-context call — no trade refresh tokens. Fail-safe: None."""
        from datetime import date as _date, timedelta
        lookback = max(1, int(lookback))
        today = _date.today()
        start = (today - timedelta(days=lookback * 2 + 10)).strftime("%Y-%m-%d")
        end = today.strftime("%Y-%m-%d")
        try:
            # max_count must cover the ENTIRE [start, end] window: the SDK returns
            # bars in ascending (oldest-first) order and truncates from the front
            # when max_count is smaller than the window.  Using just lookback+5 would
            # drop the most-recent bars — the ones we actually want.
            ret, data, _pk = self._quote.request_history_kline(
                symbol, start=start, end=end, ktype=self._c.KLType.K_DAY,
                autype=self._c.AuType.QFQ, max_count=2 * lookback + 15)
        except Exception as e:                       # never raise into the loop
            logger.warning("recent_high %s: kline request raised: %s", symbol, e)
            return None
        if not self._ok(ret) or self._c.is_empty(data):
            logger.info("recent_high %s: ret=%s %s", symbol, ret,
                        data if isinstance(data, str) else "empty")
            return None
        highs = []
        for i in range(len(data)):
            row = data.iloc[i]
            tk = str(self._c.safe_get(row, "time_key", "time", default=""))[:10]
            if tk == end:                            # exclude today's forming bar
                continue
            h = self._c.safe_float(self._c.safe_get(row, "high", default=0))
            if h > 0:
                highs.append(h)
        completed = highs[-lookback:]
        if len(completed) < lookback:                # insufficient history -> no entry
            logger.info("recent_high %s: only %d/%d completed bars", symbol,
                        len(completed), lookback)
            return None
        return max(completed)

    def get_option_chain(self, underlying: str, right,
                         dte_min: int = 0, dte_max: int = 100000):  # pragma: no cover — live OpenD
        """Live option chain for `underlying` (e.g. US.AAPL) and `right`
        (CALL/PUT), restricted to expiries in [today+dte_min, today+dte_max],
        returned as options.chain.OptionQuote rows.

        get_option_chain rejects any [start, end] span > 30 days, so we enumerate
        expiries once via get_option_expiration_date (no span limit), keep only
        those in the DTE window, and fetch the chain per <=30-day expiry bucket.
        All SDK/options imports are confined here (module stays SDK-free at import).
        Field names per vendored get_option_chain.py / get_option_expiration_date.py;
        safe_get tolerates the candidate keys below across SDK versions."""
        from datetime import date as _date, datetime
        from autotrader.options.chain import OptionQuote
        from moomoo import OptionType  # confined import

        opt_type = OptionType.CALL if str(right).upper() == "CALL" else OptionType.PUT
        today = _date.today()

        eret, edf = self._quote.get_option_expiration_date(underlying)
        if not self._ok(eret) or self._c.is_empty(edf):
            logger.info("get_option_expiration_date %s: ret=%s %s", underlying, eret,
                        edf if isinstance(edf, str) else "empty")
            return []
        expiries = []
        for i in range(len(edf)):
            s = str(self._c.safe_get(edf.iloc[i], "strike_time", "expiry_date", default=""))
            try:
                expiries.append(datetime.strptime(s[:10], "%Y-%m-%d").date())
            except (ValueError, TypeError):
                continue

        codes: List[str] = []
        seen = set()
        for w_start, w_end in _chain_windows(expiries, today, dte_min, dte_max):
            ret, chain = self._quote.get_option_chain(
                underlying, option_type=opt_type,
                start=w_start.strftime("%Y-%m-%d"), end=w_end.strftime("%Y-%m-%d"))
            if not self._ok(ret) or self._c.is_empty(chain):
                # A bad sub-window must not abort the others; the error payload is
                # a str (not a df) when ret != RET_OK.
                logger.info("get_option_chain %s %s %s..%s: ret=%s %s",
                            underlying, str(right).upper(), w_start, w_end, ret,
                            chain if isinstance(chain, str) else "empty")
                continue
            for i in range(len(chain)):
                code = str(self._c.safe_get(chain.iloc[i], "code", default=""))
                if code and code not in seen:
                    seen.add(code)
                    codes.append(code)
        if not codes:
            return []

        # get_market_snapshot is capped at 400 codes per call (API_LIMITS.md), and
        # liquid names easily exceed that (AAPL ~768 puts), so batch the request.
        SNAPSHOT_MAX = 400
        out = []
        for start in range(0, len(codes), SNAPSHOT_MAX):
            batch = codes[start:start + SNAPSHOT_MAX]
            sret, snap = self._quote.get_market_snapshot(batch)
            if not self._ok(sret) or self._c.is_empty(snap):
                logger.info("get_market_snapshot %s batch[%d:%d]: ret=%s %s",
                            underlying, start, start + len(batch), sret,
                            snap if isinstance(snap, str) else "empty")
                continue
            for i in range(len(snap)):
                row = snap.iloc[i]
                code = str(self._c.safe_get(row, "code", default=""))
                strike = self._c.safe_float(self._c.safe_get(
                    row, "option_strike_price", "strike_price", "strike", default=0))
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
        if orders is None:
            # Book unknown — cancelling by a stale/guessed list is worse than
            # doing nothing; the next lifecycle reconcile retries.
            logger.error("cancel_all: open-orders query failed — book unknown, nothing cancelled")
            return
        logger.info("cancel_all: %d open order(s) to cancel", len(orders))
        for ack in orders:
            if ack.broker_order_id:
                try:
                    self.cancel_order(ack.broker_order_id)
                except BrokerError:
                    pass  # best-effort flatten on shutdown; logged by cancel_order

    def get_open_orders(self) -> Optional[List[OrderAck]]:
        """Working orders, or None when the QUERY FAILED (rate-limit timeout or
        non-OK ret). None is distinct from [] (genuinely no working orders) —
        the same contract as _positions(). Callers must treat None as UNKNOWN,
        never as 'nothing is working' (a rate-limited query must not make a
        hedge look filled or a resting limit look done)."""
        if not self._refresh_rl.acquire(timeout=60.0):
            logger.warning("get_open_orders: refresh rate limit — book unknown")
            return None
        ret, data = self._trade.order_list_query(
            trd_env=self._env(), acc_id=self._acc_id, refresh_cache=True)
        if not self._ok(ret):
            logger.warning("get_open_orders: query failed: %s", data)
            return None
        if self._c.is_empty(data):
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
        pnl_raw = self._c.safe_get(acc.iloc[0], "realized_pl", "today_pnl_value",
                                   default=None)
        pnl = self._c.safe_float(pnl_raw) if pnl_raw is not None else 0.0
        day_pnl_known = pnl_raw is not None and math.isfinite(pnl)
        if not day_pnl_known:
            pnl = 0.0
            logger.warning("get_account: no realized_pl/today_pnl_value field — "
                           "day_pnl UNKNOWN (risk check will fail closed)")
        upnl = self._c.safe_float(self._c.safe_get(acc.iloc[0], "unrealized_pl", default=0))
        positions = self._positions()
        if positions is None:
            # Position query FAILED — never present a falsely-flat snapshot the
            # risk core would trust. Mark stale AND not-loaded (E1/R7).
            return AccountSnapshot(cash=cash, total_assets=total, day_pnl=pnl,
                                   stale=True, positions_loaded=False,
                                   day_pnl_known=day_pnl_known,
                                   unrealized_pnl=upnl, positions=())
        # A genuinely empty account with zero assets is also treated as stale.
        stale = (total == 0 and not positions)
        return AccountSnapshot(cash=cash, total_assets=total, day_pnl=pnl,
                               stale=stale, positions_loaded=True,
                               day_pnl_known=day_pnl_known,
                               unrealized_pnl=upnl, positions=tuple(positions))

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

    def get_open_orders_count(self) -> int:  # convenience for logs; -1 = unknown
        orders = self.get_open_orders()
        return len(orders) if orders is not None else -1

    def _is_paper(self) -> bool:
        """SIMULATE accounts have no deal/fill feed — deal_list_query returns
        ret=-1 'Paper trading does not support deal data'. Tolerates _env()
        yielding either a TrdEnv enum (live) or a plain str (tests)."""
        return self._c.format_enum(self._env()).upper() == "SIMULATE"

    def reconcile_fills(self, since: Optional[str]) -> Optional[List[Fill]]:
        # Paper/SIMULATE: the deal feed is unavailable, so reconstruct fills from
        # the order list (which IS supported on paper). Without this, the fills
        # projection stays permanently empty on paper and the EOD report shows no
        # activity despite real executions. Live accounts use the real deal feed.
        # Returns None when the underlying query FAILED — distinct from [] (no fills).
        # Callers must skip projection updates on None.
        if self._is_paper():
            return self._fills_from_orders()
        if not self._refresh_rl.acquire(timeout=60.0):
            return None
        kwargs = dict(trd_env=self._env(), acc_id=self._acc_id, refresh_cache=True)
        if since:
            kwargs["begin_time"] = since
        ret, data = self._trade.deal_list_query(**kwargs)
        if not self._ok(ret):
            logger.warning("reconcile_fills: deal query failed: %s", data)
            return None
        if self._c.is_empty(data):
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

    def _fills_from_orders(self) -> Optional[List[Fill]]:
        """Synthesize fills from the order list for paper accounts: one Fill per
        order with executed quantity (dealt_qty > 0), keyed by order_id so it is
        idempotent across a day's reconciles (db.record_fills dedupes by fill_id).

        Returns None when the order query FAILED — distinct from [] (no fills).

        Caveat: a partial fill is captured at its dealt_qty as of this reconcile;
        the stable fill_id means a later top-up is not re-counted. Paper market
        orders fill atomically, and reconciles run after the entry window closes
        (orders terminal), so in practice this matches the real executed quantity."""
        if not self._refresh_rl.acquire(timeout=60.0):
            return None
        ret, data = self._trade.order_list_query(
            trd_env=self._env(), acc_id=self._acc_id, refresh_cache=True)
        if not self._ok(ret):
            logger.warning("_fills_from_orders: order query failed: %s", data)
            return None
        if self._c.is_empty(data):
            return []
        out: List[Fill] = []
        for i in range(len(data)):
            row = data.iloc[i]
            dealt_qty = self._c.safe_float(self._c.safe_get(row, "dealt_qty", default=0))
            if dealt_qty <= 0:
                continue  # submitted/rejected/cancelled with no execution -> no fill
            order_id = str(self._c.safe_get(row, "order_id", "orderID", default=""))
            if not order_id:
                continue
            side_raw = self._c.format_enum(self._c.safe_get(row, "trd_side", default="BUY")).upper()
            out.append(Fill(
                fill_id=f"paper-{order_id}",
                symbol=str(self._c.safe_get(row, "code", default="")),
                side="BUY" if side_raw == "BUY" else "SELL",
                qty=dealt_qty,
                price=self._c.safe_float(self._c.safe_get(row, "dealt_avg_price", default=0)),
                ts=str(self._c.safe_get(row, "updated_time", "create_time", default="")),
            ))
        return out
