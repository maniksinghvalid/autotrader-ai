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

import json
import logging
from dataclasses import dataclass
from datetime import date
from typing import Callable, Optional

from autotrader.domain import BrokerError, is_option_symbol
from autotrader.rebalance import RebalanceTrade

logger = logging.getLogger("autotrader.stops")


@dataclass(frozen=True)
class StopReconcileResult:
    attached: int
    attach_failed: int
    orphans_cancelled: int
    already_protected: int
    skipped_no_quote: int
    simulated: int = 0


class StopManager:
    def __init__(self, engine, broker, db, cfg,
                 alert_fn: Optional[Callable[[str], None]] = None):
        self._engine = engine
        self._b = broker
        self._db = db
        self._cfg = cfg
        self._alert = alert_fn
        # once-per-(symbol, day) dedup for failed-trigger alerts — the alert_fn
        # lambda has no dedup of its own and the sweep retries every interval
        self._trigger_alerted: set = set()

    def _sim_enabled(self) -> bool:
        """Simulated stops exist ONLY because Moomoo paper rejects broker-side
        TRAILING_STOP orders. Live keeps real broker stops — never simulate."""
        return (self._cfg.trading_env == "PAPER"
                and self._cfg.trailing_stop_pct > 0)

    def _held(self, snap) -> dict:
        held = {p.symbol: p.qty for p in snap.positions
                if p.qty > 0 and not is_option_symbol(p.symbol)}
        # SHARED (V11, C6): AutoTrader shares this paper account with an
        # independent SNP bot. Every account-wide operation scopes to ONLY our
        # own tracked book — the SNP bot's orders/positions are never cancelled
        # or stop-managed. SOLE (default) is unchanged.
        if getattr(self._cfg, "account_ownership", "SOLE") == "SHARED":
            owned = self._db.owned_symbols()
            held = {s: q for s, q in held.items() if s in owned}
        return held

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
        held = self._held(snap)
        shared = getattr(self._cfg, "account_ownership", "SOLE") == "SHARED"

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
        attached = failed = protected = skipped = simulated = 0
        for symbol in sorted(held):
            qty = held[symbol]
            existing = self._db.get_open_trailing_stop(symbol)
            if existing is not None and existing in surviving:
                protected += 1
                continue
            if self._sim_enabled() and \
                    self._db.get_state(f"simstop:{symbol}") is not None:
                # already covered by a simulated stop (paper) — the broker
                # attach is known to reject; don't retry or alert every
                # reconcile. Daily HWM reseed happens in check_simulated.
                simulated += 1
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
            elif self._sim_enabled():
                # Moomoo paper rejects TRAILING_STOP orders — fall back to an
                # engine-side simulated stop: persist a high-water mark seeded
                # at the same price a broker stop would have started trailing
                # from; check_simulated ratchets it and fires the exit.
                self._db.set_state(
                    f"simstop:{symbol}",
                    json.dumps({"hwm": price, "since": today.isoformat()}))
                simulated += 1
                logger.info("simulated trailing stop armed: %s hwm=%.2f "
                            "trail=%.1f%%", symbol, price,
                            self._cfg.trailing_stop_pct)
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
                    "protected=%d skipped=%d simulated=%d", attached, failed,
                    orphans, protected, skipped, simulated)
        return StopReconcileResult(attached, failed, orphans, protected,
                                   skipped, simulated)

    def check_simulated(self, now) -> int:
        """Sweep the armed simulated stops (paper only): ratchet each symbol's
        high-water mark from the current quote and fire a full-position SELL
        through the engine's audited path when price breaches
        hwm * (1 - trailing_stop_pct/100). Returns the number fired. Runs every
        ~AUTOTRADER_SIMSTOP_INTERVAL seconds from the session loop while
        entries are enabled."""
        if not self._sim_enabled():
            return 0
        rows = self._db.list_state("simstop:")
        if not rows:
            return 0
        snap = self._b.get_account()
        if not snap.positions_loaded or snap.stale:
            logger.warning("simulated stops: positions unavailable/stale — no-op")
            return 0
        held = self._held(snap)
        today = now.date().isoformat()
        fired = 0
        for key, raw in rows:
            symbol = key.split(":", 1)[1]
            if symbol not in held:
                # sold externally / halt-flattened / trigger filled pre-crash —
                # nothing left to protect
                self._db.delete_state(key)
                continue
            quote = self._b.get_quote(symbol)
            if quote is None:
                continue                      # transient — retry next sweep
            state = json.loads(raw)
            if state.get("since") != today:
                # Daily reseed to the morning quote — live parity: a DAY-TIF
                # broker stop is re-attached each morning and trails from
                # there, so an overnight gap-down must not insta-fire.
                self._db.set_state(key, json.dumps({"hwm": quote, "since": today}))
                continue
            hwm = float(state["hwm"])
            if quote > hwm:
                self._db.set_state(key, json.dumps({"hwm": quote, "since": today}))
                continue
            if quote <= hwm * (1 - self._cfg.trailing_stop_pct / 100.0):
                # ponytail: sell full current qty at trigger; per-lot stops if
                # partial-exit strategies ever exist
                res = self._engine.submit_rebalance_order(
                    RebalanceTrade(symbol, "SELL", held[symbol], "STOP", 0),
                    quote, f"simstop-{today}")
                if res.action == "ORDER_PLACED":
                    self._db.delete_state(key)
                    fired += 1
                    logger.info("simulated trailing stop FIRED: %s SELL %d "
                                "@ %.2f (hwm %.2f, trail %.1f%%)", symbol,
                                held[symbol], quote, hwm,
                                self._cfg.trailing_stop_pct)
                else:
                    # keep the KV — still unprotected, retries next sweep
                    msg = (f"⚠ simulated trailing stop for {symbol} triggered "
                           f"but the exit SELL failed ({res.action}) — "
                           f"position still unprotected")
                    logger.error("%s (%s)", msg, res.detail)
                    if self._alert is not None and (symbol, today) not in self._trigger_alerted:
                        self._trigger_alerted.add((symbol, today))
                        try:
                            self._alert(msg)
                        except Exception as e:  # alerting must never break the loop
                            logger.error("simulated stops: alert failed: %s", e)
        return fired
