"""Stop lifecycle as a first-class concern (spec W1).

EOD cancels every working order while positions carry overnight, and Moomoo
orders are DAY time-in-force anyway — so protective trailing stops MUST be
re-attached every morning or carried positions are unprotected from day 2
(review Critical #1). StopManager reconciles "intended protection" (every held
equity long has exactly one working trailing stop) against "working orders"
(broker truth): it cancels orphans first (stops for closed positions, leftover
option legs, anything unrecognized — this also closes the option-leg
orphan-rest pre-live blocker), then attaches missing stops through the
engine's audited path. Unknown broker state (failed open-orders or positions
query) makes the whole reconcile a logged no-op — never attach or cancel
against an unknown book."""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date
from typing import Callable, Optional

from autotrader.domain import BrokerError, is_option_symbol

logger = logging.getLogger("autotrader.stops")


@dataclass(frozen=True)
class StopReconcileResult:
    attached: int
    attach_failed: int
    orphans_cancelled: int
    already_protected: int
    skipped_no_quote: int


class StopManager:
    def __init__(self, engine, broker, db, cfg,
                 alert_fn: Optional[Callable[[str], None]] = None):
        self._engine = engine
        self._b = broker
        self._db = db
        self._cfg = cfg
        self._alert = alert_fn

    def reconcile(self, today: date) -> Optional[StopReconcileResult]:
        open_orders = self._b.get_open_orders()
        if open_orders is None:
            logger.warning("stop reconcile: open-orders query failed — no-op "
                           "(retries next lifecycle run)")
            return None
        snap = self._b.get_account()
        if not snap.positions_loaded or snap.stale:
            logger.warning("stop reconcile: positions unavailable/stale — no-op")
            return None
        held = {p.symbol: p.qty for p in snap.positions
                if p.qty > 0 and not is_option_symbol(p.symbol)}

        # SHARED (V11, C6): AutoTrader shares this paper account with an
        # independent SNP bot. Every account-wide operation below scopes to
        # ONLY our own tracked book — the SNP bot's orders/positions are never
        # cancelled or stop-managed. SOLE (default) is unchanged.
        shared = getattr(self._cfg, "account_ownership", "SOLE") == "SHARED"
        if shared:
            owned = self._db.owned_symbols()
            held = {s: q for s, q in held.items() if s in owned}

        # Pass 1 — orphan sweep, BEFORE attaching (so a fresh stop is never
        # swept by its own reconcile): cancel every working order that is not
        # a recognized trailing stop protecting a held long.
        orphans = 0
        for ack in open_orders:
            boid = ack.broker_order_id
            if not boid:
                continue
            row = self._db.get_trade_by_broker_order_id(boid)
            if shared and row is None:
                logger.info("stop reconcile: foreign order %s left untouched "
                            "(SHARED ownership)", boid)
                continue
            keep = (row is not None and row[2] == "TRAILING_STOP"
                    and row[1] == "SELL" and row[0] in held)
            if keep:
                continue
            try:
                self._b.cancel_order(boid)
            except BrokerError as e:
                logger.error("stop reconcile: cancel orphan %s failed: %s", boid, e)
                continue
            self._db.mark_order_cancelled(boid)
            orphans += 1
            logger.warning("stop reconcile: cancelled orphan order %s (%s)",
                           boid, row if row else "not in trades projection")

        if self._cfg.trailing_stop_pct <= 0:
            logger.info("stop reconcile: trailing stops disabled — attach pass skipped")
            return StopReconcileResult(0, 0, orphans, 0, 0)

        # Pass 2 — attach: every held equity long gets one working stop.
        surviving = {a.broker_order_id for a in open_orders if a.broker_order_id}
        surviving -= {None}
        attached = failed = protected = skipped = 0
        for symbol in sorted(held):
            qty = held[symbol]
            existing = self._db.get_open_trailing_stop(symbol)
            if existing is not None and existing in surviving:
                protected += 1
                continue
            price = self._b.get_quote(symbol)
            if price is None:
                skipped += 1
                msg = (f"⚠ UNPROTECTED — {symbol} long {qty} has no working "
                       f"trailing stop and no quote is available to attach one.")
                logger.error(msg)
                if self._alert is not None:
                    try:
                        self._alert(msg)
                    except Exception as e:  # alerting must never break the loop
                        logger.error("stop reconcile: alert failed: %s", e)
                continue
            if self._engine.attach_trailing_stop(
                    symbol, qty, price, f"reattach-{today.isoformat()}"):
                attached += 1
            else:
                failed += 1
                msg = (f"⚠ UNPROTECTED — {symbol} long {qty} has no working "
                       f"trailing stop and the morning re-attach was rejected.")
                logger.error(msg)
                if self._alert is not None:
                    try:
                        self._alert(msg)
                    except Exception as e:  # alerting must never break the loop
                        logger.error("stop reconcile: alert failed: %s", e)
        logger.info("stop reconcile: attached=%d failed=%d orphans=%d "
                    "protected=%d skipped=%d", attached, failed, orphans,
                    protected, skipped)
        return StopReconcileResult(attached, failed, orphans, protected, skipped)
