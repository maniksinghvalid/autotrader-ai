"""Orchestration. One deterministic tick: snapshot -> quote -> strategy ->
confidence filter -> risk core -> router. Shutdown cancels all open orders
before disconnect (CLAUDE.md). The engine takes any Broker, so it is testable
against SimBroker with no OpenD. The __main__ path wires a live MoomooBroker
behind is_opend_ready()."""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date
from typing import TYPE_CHECKING, List, Optional, Tuple

from autotrader.broker import Broker
from autotrader.config import RiskConfig, load_risk_config
from autotrader.domain import BrokerError, OrderRequest, OrderState, Signal
from autotrader.rebalance import compute_plan
from autotrader.risk_check import evaluate as risk_evaluate, RiskAction
from autotrader.risk_core import evaluate
from autotrader.router import OrderRouter
from autotrader.sizing import size_position
from autotrader.strategies.threshold import ThresholdStrategy
from autotrader.watchdog import backoff_seconds

if TYPE_CHECKING:
    from autotrader.db import DB
    from autotrader.lifecycle import EntryGate

logger = logging.getLogger("autotrader.engine")

_HEDGE_CONFIRM_ATTEMPTS = 3   # bounded live fill-poll (paper fills at ack: 0 polls)


def select_strategy_symbol(allowed_symbols, override: "Optional[str]" = None,
                           default: str = "US.AAPL") -> str:
    """Pick the internal strategy's single symbol deterministically.

    `next(iter(frozenset))` is NOT stable across process restarts (Python string-hash
    randomization), so the strategy silently traded a different holding each run. An
    explicit STRATEGY_SYMBOL override wins when it is in the allowlist; otherwise the
    lexicographically smallest allowed symbol is used (stable across restarts). Falls
    back to `default` only when the allowlist is empty."""
    if override:
        ov = override.strip().upper()
        if ov in allowed_symbols:
            return ov
        logger.warning("STRATEGY_SYMBOL=%r not in RISK_ALLOWED_SYMBOLS — ignoring, "
                       "using deterministic pick", override)
    if not allowed_symbols:
        return default
    return sorted(allowed_symbols)[0]


def _post_slack(url, payload):
    # Lazy import keeps reporting (and its stdlib urllib use) out of the hot
    # import path for tests that never touch alerts.
    from autotrader.reporting.eod_reporter import post_slack
    return post_slack(url, payload)


@dataclass(frozen=True)
class TickResult:
    action: str
    detail: str = ""


class TradeEngine:
    def __init__(self, broker: Broker, strategy: ThresholdStrategy, cfg: RiskConfig,
                 order_qty: int, audit_path: str, db: "Optional[DB]" = None,
                 entry_gate: "Optional[EntryGate]" = None, today_fn=None,
                 hedge_confirm_attempts: int = _HEDGE_CONFIRM_ATTEMPTS,
                 hedge_confirm_sleep=None,
                 escalation_sleep=None,
                 alert_url: "Optional[str]" = None, alert_post=None,
                 session_id: "Optional[str]" = None, snapshot_cache_ticks: int = 1,
                 strategy_enabled: bool = True, breakout_ref=None):
        self._b = broker
        self._strat = strategy
        self._strategy_enabled = strategy_enabled
        self._breakout_ref = breakout_ref
        self._cfg = cfg
        self._qty = order_qty
        self._router = OrderRouter(broker, audit_path=audit_path)
        self._signal_seq = 0
        # Per-session prefix so signal ids stay unique ACROSS process restarts. The
        # seq resets to 1 each start; signals.signal_id is UNIQUE and record_signal
        # is INSERT OR IGNORE, so a bare "sig-N" silently collides with a prior run's
        # row (and, via make_client_order_id, risks cid collisions). Default is a
        # fresh nonce per engine/process; tests may inject a fixed id.
        import uuid
        self._session_id = session_id or uuid.uuid4().hex[:8]
        # Refresh-token budgeting: the tick's account snapshot may be reused for
        # up to snapshot_cache_ticks ticks. get_account costs 2 refresh tokens
        # (accinfo + positions) against Moomoo's 10-per-30s budget, and a 5s loop
        # fetching every tick starves hedge-confirm/escalation queries. Cached
        # data feeds ONLY the strategy evaluate; routing always re-fetches.
        self._snap_cache_ticks = max(1, int(snapshot_cache_ticks))
        self._snap_cache = None
        self._snap_cache_left = 0
        self._db = db
        self._gate = entry_gate
        self._today_fn = today_fn or date.today
        self._hedge_confirm_attempts = hedge_confirm_attempts
        # Injected delay between fill-poll attempts. Defaults to a no-op so unit
        # tests never really sleep; live wiring passes time.sleep. This is an
        # order-FILL poll, not an OpenD readiness check (CLAUDE.md), and reuses the
        # injected-sleep + backoff_seconds pattern already used by watchdog.py.
        self._hedge_confirm_sleep = hedge_confirm_sleep or (lambda _s: None)
        # Injected dwell between escalation stages — same pattern as
        # hedge_confirm_sleep: no-op in tests, time.sleep in production wiring.
        self._escalation_sleep = escalation_sleep or (lambda _s: None)
        self._alert_url = alert_url
        self._alert_post = alert_post
        # External BUY signals that arrived while the entry window was closed
        # (e.g. a routine that fires pre-market). Held here rather than dropped —
        # the inbox consumes each drop once, so a dropped BUY is lost forever —
        # and replayed by flush_deferred_entries() when entries open. Keyed latest-
        # wins per (symbol, direction) so the queue cannot grow unbounded and a
        # stale duplicate never fires alongside a fresh signal.
        # (deferred_on ISO date, Signal). Persisted to DB.engine_state (W4) so a
        # crash between a pre-market defer and the 09:45 flush cannot lose the
        # signal; rows from an earlier session date EXPIRE rather than replaying
        # ~18h stale.
        self._deferred_entries: "List[Tuple[str, Signal]]" = []
        if self._db is not None:
            self._restore_deferred_entries()

    def _restore_deferred_entries(self) -> None:
        import json
        today = self._today_fn().isoformat()
        for key, raw in self._db.list_state("deferred:"):
            try:
                d = json.loads(raw)
            except ValueError:
                logger.error("deferred entry %s: unreadable payload — dropped", key)
                self._db.delete_state(key)
                continue
            if d.get("deferred_on") != today:
                logger.warning("deferred entry %s expired (deferred_on=%s, today=%s) "
                               "— dropped, not routed", key, d.get("deferred_on"), today)
                self._db.delete_state(key)
                continue
            self._deferred_entries.append((d["deferred_on"], Signal(
                symbol=d["symbol"], direction=d["direction"],
                confidence=d["confidence"], rationale=d["rationale"],
                stop_price=d.get("stop_price"))))

    def tick(self) -> TickResult:
        if self._gate is not None and self._gate.halted:
            return TickResult("HALTED")
        if not self._strategy_enabled:
            return TickResult("STRATEGY_DISABLED")
        snap = self._account_for_tick()
        symbol = self._strat.p.symbol
        price = self._b.get_quote(symbol)
        if price is None:
            return TickResult("NO_QUOTE", symbol)

        pos = next((p for p in snap.positions if p.symbol == symbol), None)
        ref_high = self._breakout_ref.high(symbol) if self._breakout_ref is not None else None
        signal = self._strat.evaluate(price=price, position=pos, ref_high=ref_high)
        if signal is None:
            return TickResult("NO_SIGNAL")
        # Routing decisions never run on cached data: drop the cache and
        # re-fetch fresh so the risk core sees current positions/exposure.
        self._snap_cache = None
        snap = self._b.get_account()
        return self._route_signal(signal, snap, price)

    def submit_external_signal(self, signal: Signal) -> TickResult:
        """Route a validated external signal through the SAME pipeline as a
        strategy signal: confidence filter -> entry gate -> risk core -> router.
        External signals NEVER bypass the risk core (research C4)."""
        self._snap_cache = None   # external routes invalidate the tick cache
        snap = self._b.get_account()
        price = self._b.get_quote(signal.symbol)
        if price is None:
            return TickResult("NO_QUOTE", signal.symbol)
        res = self._route_signal(signal, snap, price)
        # A pre-market plain-equity BUY that cleared the confidence filter but hit the
        # closed entry gate is deferred, not dropped — held until entries open and then
        # replayed against a fresh snapshot/quote by flush_deferred_entries(). Scoped to
        # non-overlay entries: option overlays keep their own gating (they record before
        # their gate check, so deferral there would double-write) — that is a separate
        # follow-up if pre-market overlay routines need the same treatment.
        if res.action == "ENTRY_CLOSED" and signal.overlay is None:
            self._defer_entry(signal)
            return TickResult("ENTRY_DEFERRED", signal.symbol)
        return res

    def _defer_entry(self, signal: Signal) -> None:
        """Queue an external entry for the next open, latest-wins per (symbol,
        direction), persisted so a restart cannot lose it (W4)."""
        deferred_on = self._today_fn().isoformat()
        self._deferred_entries = [
            e for e in self._deferred_entries
            if (e[1].symbol, e[1].direction) != (signal.symbol, signal.direction)
        ]
        self._deferred_entries.append((deferred_on, signal))
        if self._db is not None:
            import json
            self._db.set_state(
                f"deferred:{signal.symbol}:{signal.direction}",
                json.dumps({"symbol": signal.symbol, "direction": signal.direction,
                            "confidence": signal.confidence,
                            "rationale": signal.rationale,
                            "stop_price": signal.stop_price,
                            "deferred_on": deferred_on}))

    def flush_deferred_entries(self) -> "List[TickResult]":
        """Replay entries deferred while the window was closed. No-op unless
        entries are now open. Each is re-routed through submit_external_signal
        (re-sized, re-risk-checked against CURRENT data). Entries deferred on an
        EARLIER session date expire here instead of replaying stale."""
        if self._gate is None or not self._gate.entries_enabled:
            return []
        pending, self._deferred_entries = self._deferred_entries, []
        today = self._today_fn().isoformat()
        results: "List[TickResult]" = []
        for deferred_on, sig in pending:
            key = f"deferred:{sig.symbol}:{sig.direction}"
            if deferred_on != today:
                logger.warning("deferred %s %s expired (deferred_on=%s) — not routed",
                               sig.direction, sig.symbol, deferred_on)
                if self._db is not None:
                    self._db.delete_state(key)
                continue
            results.append(self.submit_external_signal(sig))
            if self._db is not None:
                self._db.delete_state(key)
        if pending:
            logger.info("flushed %d deferred entry(ies) at entry-open", len(results))
        return results

    def _account_for_tick(self):
        if self._snap_cache is None or self._snap_cache_left <= 0:
            self._snap_cache = self._b.get_account()
            self._snap_cache_left = self._snap_cache_ticks
        self._snap_cache_left -= 1
        return self._snap_cache

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

        # W7: performance rows are written ONLY by SessionRunner._record_perf
        # (fills-derived realized, quote-based unrealized) — never from engine
        # paths with raw broker figures the EOD report distrusts.

        self._signal_seq += 1
        signal_id = f"sig-{self._session_id}-{self._signal_seq}"
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
        otype, lpx = self._order_kind(signal.direction, price)
        req = OrderRequest(symbol=signal.symbol, side=signal.direction, qty=eff_qty,
                           order_type=otype, limit_price=lpx, client_order_id=cid)

        decision = evaluate(req, snap, self._cfg, ref_price=price)
        if not decision.approved:
            logger.warning("risk core rejected: %s", decision.reason)
            return TickResult("REJECTED_BY_RISK", decision.reason)

        ack, term = self._submit_with_escalation(req, snap, price)
        if self._db:
            self._db.record_trade(
                client_order_id=ack.client_order_id, symbol=req.symbol, side=req.side,
                qty=req.qty, order_type=term.order_type, limit_price=term.limit_price,
                broker_order_id=ack.broker_order_id, state=ack.state.value,
            )
        if ack.state is OrderState.UNKNOWN:
            return TickResult("ORDER_UNKNOWN", ack.client_order_id)
        if ack.state is OrderState.REJECTED:
            return TickResult("ORDER_REJECTED", ack.client_order_id)

        # Broker-resting trailing stop: attach a protective TRAILING_STOP SELL
        # right after a BUY entry places (research R5 — survives an OpenD outage).
        if signal.direction == "BUY" and self._cfg.trailing_stop_pct > 0:
            self._attach_trailing_stop(signal.symbol, eff_qty, price, signal_id,
                                       entry_ack=ack)
        return TickResult("ORDER_PLACED", str(ack.broker_order_id))

    def _order_kind(self, side, ref_price: float):
        """(order_type, limit_price) for an equity entry/exit/rebalance leg.
        Flag OFF -> ('MARKET', None), byte-for-byte today's behavior. Flag ON ->
        a capped marketable LIMIT around ref_price. Trailing stops and option legs
        do NOT use this."""
        if not self._cfg.limit_orders_enabled:
            return "MARKET", None
        from autotrader.limit_pricing import capped_limit_price  # local: keep import cheap
        return "LIMIT", capped_limit_price(side, ref_price, self._cfg)

    def _order_working(self, ack) -> "Optional[bool]":
        """Tri-state open-orders membership for ack's client_order_id.
        True = still working; False = off the book (filled/terminal); None =
        the query FAILED — the caller must treat the state as UNKNOWN, never
        as 'filled' (review Important #1)."""
        open_orders = self._b.get_open_orders()
        if open_orders is None:
            return None
        return ack.client_order_id in {o.client_order_id for o in open_orders}

    def _cancel_for_escalation(self, ack) -> bool:
        """Request the cancel, then CONFIRM the order actually left the book
        before allowing the next stage (V5a): RET_OK acks the REQUEST — an
        in-flight fill between request and effect would double-fill if the
        next stage submitted immediately. Bounded confirm poll; unknown book
        or still-working => False (stop escalating; EOD/reconcile cleans up)."""
        if not ack.broker_order_id:
            return False
        try:
            self._b.cancel_order(ack.broker_order_id)
        except BrokerError as e:
            logger.warning("escalation: cancel %s failed (%s) — NOT advancing",
                           ack.broker_order_id, e)
            return False
        for attempt in range(1, 4):
            self._escalation_sleep(backoff_seconds(attempt))
            working = self._order_working(ack)
            if working is False:
                return True          # confirmed off the book
            if working is None:
                logger.warning("escalation: open-orders unknown while confirming "
                               "cancel of %s — NOT advancing", ack.broker_order_id)
                return False
        logger.warning("escalation: %s still working after cancel request — "
                       "NOT advancing", ack.broker_order_id)
        return False

    def _submit_with_escalation(self, req: OrderRequest, snap, ref_price: float):
        """Submit a capped LIMIT; give it escalation_dwell_seconds to fill; if
        still resting, cancel + re-peg once to the current touch; dwell again;
        if still resting, submit MARKET so a risk exit completes. Each stage
        re-runs the risk core and uses a distinct client_order_id. An UNKNOWN
        open-orders book or a FAILED cancel aborts escalation with the current
        ack — a duplicate fill is worse than a resting limit.

        Returns (ack, terminal_req): terminal_req produced the returned ack."""
        if req.order_type != "LIMIT":
            return self._router.submit(req), req
        ack = self._router.submit(req)
        ack_req = req

        self._escalation_sleep(self._cfg.escalation_dwell_seconds)
        working = self._order_working(ack)
        if working is None:
            logger.warning("escalation: open-orders unknown — leaving %s as-is",
                           ack.client_order_id)
            return ack, req
        if not working:
            return ack, req   # filled (or terminal) within the dwell
        if not self._cancel_for_escalation(ack):
            return ack, req

        # Stage 2: re-peg to the CURRENT touch (original order confirmed cancelled).
        from autotrader.limit_pricing import capped_limit_price
        cur = self._b.get_quote(req.symbol) or ref_price
        peg_cid = req.client_order_id + "-peg"
        peg = OrderRequest(symbol=req.symbol, side=req.side, qty=req.qty,
                           order_type="LIMIT",
                           limit_price=capped_limit_price(req.side, cur, self._cfg),
                           client_order_id=peg_cid, option=req.option,
                           position_effect=req.position_effect,
                           correlation_id=req.correlation_id)
        if evaluate(peg, snap, self._cfg, ref_price=cur).approved:
            ack = self._router.submit(peg)
            ack_req = peg
            self._escalation_sleep(self._cfg.escalation_dwell_seconds)
            working = self._order_working(ack)
            if working is None:
                logger.warning("escalation: open-orders unknown after re-peg — "
                               "leaving %s as-is", ack.client_order_id)
                return ack, peg
            if not working:
                return ack, peg
            if not self._cancel_for_escalation(ack):
                return ack, ack_req

        # Stage 3: MARKET fallback — reached only with no order left resting.
        mkt_cid = req.client_order_id + "-mkt"
        mkt = OrderRequest(symbol=req.symbol, side=req.side, qty=req.qty,
                           order_type="MARKET", limit_price=None,
                           client_order_id=mkt_cid, option=req.option,
                           position_effect=req.position_effect,
                           correlation_id=req.correlation_id)
        # Price the MARKET stage's risk check at the side it will actually
        # execute (BUY lifts the ask, SELL hits the bid) rather than the last
        # quote — this makes the veto branch reachable and honest (spec W5).
        touch = self._b.get_touch(req.symbol)
        if touch is not None:
            bid, ask = touch
            exec_ref = ask if req.side == "BUY" else bid
        else:
            exec_ref = cur
        decision = evaluate(mkt, snap, self._cfg, ref_price=exec_ref)
        if not decision.approved:
            logger.warning("escalation: MARKET fallback rejected by risk: %s",
                           decision.reason)
            return ack, ack_req
        return self._router.submit(mkt), mkt

    def _route_overlay(self, signal: Signal, snap) -> TickResult:
        """Expand an overlay signal into legs and place each through the audited
        router. Legs are ordered long-before-short so a covered structure's hedge
        is never momentarily naked. O1 overlays are single-leg; multi-leg
        atomic-unwind on partial failure is O2."""
        # lazy import: keeps the options subpackage optional (matches rebalance()/_flatten_all())
        from autotrader.options.planner import build_overlay_plan, OverlayPlan

        self._signal_seq += 1
        signal_id = f"sig-{self._session_id}-{self._signal_seq}"
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

        stock_leg = next((l for l in plan.legs if l.request.option is None), None)
        hedge_legs = [l for l in plan.legs if l.request.option is not None]

        last_boid = None
        filled_long = []  # symbols of long legs already filled this overlay

        def _residual(failed_symbol: str, why: str) -> TickResult:
            logger.warning("overlay %s left long-only residual %s after %s on %s",
                           plan.correlation_id, filled_long, why, failed_symbol)
            return TickResult("OVERLAY_RESIDUAL_LONG",
                              f"{plan.correlation_id}:{','.join(filled_long)}")

        # Submit hedge legs first (BUY-before-SELL so a short is never momentarily
        # naked), THEN the stock leg last (§2.A inversion). Track whether each hedge
        # leg CONFIRMED (FILLED) for the §2.B fallback below.
        hedge_confirmed = {}   # symbol -> bool (True only once confirmed FILLED)
        hedge_states = {}      # symbol -> observed OrderState at/after placement
        ordered = sorted(hedge_legs, key=lambda l: 0 if l.request.side == "BUY" else 1)
        if stock_leg is not None:
            ordered = ordered + [stock_leg]
        for leg in ordered:
            req = leg.request
            is_stock = req.option is None
            # §2.A — HEDGE-BEFORE-STOCK: hedge legs are ordered ahead of the stock
            # leg (see `ordered` above), so a rejected/unplaceable hedge leg is
            # always caught here BEFORE the stock leg is ever reached — no naked
            # stock is structurally possible. For a plan WITH a stock leg, a hedge
            # leg failure (risk-rejected or broker-rejected/unknown) reports the
            # dedicated OVERLAY_HEDGE_UNPLACEABLE action instead of the generic
            # stock-less codes, and nothing further is submitted.
            decision = evaluate(req, snap, self._cfg, ref_price=leg.quote.premium,
                                coverage_legs=coverage)
            if not decision.approved:
                if not is_stock and stock_leg is not None:
                    logger.warning("overlay %s hedge unplaceable (%s): %s — "
                                   "skipping stock entry (no naked)",
                                   plan.correlation_id, req.symbol, decision.reason)
                    return TickResult("OVERLAY_HEDGE_UNPLACEABLE",
                                      f"{plan.correlation_id}:{req.symbol}")
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
                if not is_stock and stock_leg is not None:
                    logger.warning("overlay %s hedge unplaceable (%s): %s — "
                                   "skipping stock entry (no naked)",
                                   plan.correlation_id, req.symbol, ack.state.value)
                    return TickResult("OVERLAY_HEDGE_UNPLACEABLE",
                                      f"{plan.correlation_id}:{req.symbol}")
                if filled_long:
                    return _residual(req.symbol, ack.state.value)
                action = "ORDER_UNKNOWN" if ack.state is OrderState.UNKNOWN else "ORDER_REJECTED"
                return TickResult(action, ack.client_order_id)
            if not is_stock:
                # §2.B live-safe: FILLED at ack (paper SimBroker) confirms
                # immediately; a live SUBMITTED ack is confirmed only if a bounded
                # fill-poll shows it left the open-orders book (async fill).
                confirmed = self._confirm_hedge_fill(ack, req)
                hedge_confirmed[req.symbol] = confirmed
                # observed terminal indicator for the alert: FILLED if confirmed,
                # else the placement ack state (e.g. SUBMITTED still resting).
                hedge_states[req.symbol] = (
                    OrderState.FILLED if confirmed else ack.state)
                if (req.side == "BUY" and req.position_effect == "OPEN"
                        and confirmed):
                    filled_long.append(req.symbol)
            else:
                # §2.B — stock leg placed. If it FILLED but any hedge leg is not
                # CONFIRMED (FILLED), attach the protective trailing stop fallback
                # and fire an immediate UNHEDGED alert.
                if ack.state is OrderState.FILLED and hedge_legs and not all(
                        hedge_confirmed.get(h.request.symbol, False) for h in hedge_legs):
                    self._handle_unhedged_stock(plan, req, leg.quote.premium,
                                                signal_id, hedge_confirmed,
                                                hedge_states)
            last_boid = ack.broker_order_id
        return TickResult("OVERLAY_PLACED", f"{plan.correlation_id}:{last_boid}")

    def _confirm_off_book(self, ack) -> bool:
        """True iff ack's order is confirmed OFF the open-orders book (filled/
        terminal). FILLED at ack (paper) -> True immediately; else bounded poll
        with injected backoff sleep. None (query failed) never counts as off."""
        if ack.state is OrderState.FILLED:
            return True
        first = self._order_working(ack)
        if first is False:
            return True   # already off the book (terminal/filled) at first look
        for attempt in range(1, self._hedge_confirm_attempts + 1):
            self._hedge_confirm_sleep(backoff_seconds(attempt))
            state = self._order_working(ack)
            if state is False:
                return True
            if state is None:
                logger.warning("fill-confirm: open-orders query failed on "
                               "attempt %d — cannot confirm", attempt)
        return False

    def _confirm_hedge_fill(self, ack, req) -> bool:
        """True iff the hedge option leg is confirmed FILLED. Thin wrapper over
        _confirm_off_book — REJECTED/UNKNOWN acks were already handled by the
        caller before this runs, so a still-resting SUBMITTED after the window
        returns False -> §2.B fallback. Detection convention matches §3
        escalation (client_order_id membership)."""
        confirmed = self._confirm_off_book(ack)
        if not confirmed:
            logger.warning("hedge %s still resting after %d fill-poll attempts (%s)",
                           req.symbol, self._hedge_confirm_attempts, ack.state.value)
        return confirmed

    def _handle_unhedged_stock(self, plan, stock_req, ref_price, signal_id,
                               hedge_confirmed, hedge_states) -> None:
        """§2.B: the stock leg FILLED but its hedge is NOT confirmed — no hedge leg
        reached FILLED within the bounded fill-poll (still resting SUBMITTED, or
        REJECTED/UNKNOWN). (1) Attach the protective trailing-stop as a fallback risk
        control via the existing audited path, and (2) fire an immediate
        execution-time UNHEDGED Slack alert that names the ACTUAL observed terminal
        state of each unconfirmed leg. NEVER raises into the loop."""
        # (1) protective trailing-stop fallback (idempotent at the router via cid).
        self._attach_trailing_stop(stock_req.symbol, stock_req.qty, ref_price,
                                   signal_id)
        # (2) immediate UNHEDGED alert (outbound-only; no broker handle). Render the
        # real state observed per unconfirmed leg (e.g. "…P190000=SUBMITTED"), so a
        # live "still resting" reads distinctly from a paper "REJECTED".
        unconfirmed = [h.request.symbol for h in plan.legs
                       if h.request.option is not None
                       and not hedge_confirmed.get(h.request.symbol, False)]
        def _state_label(sym: str) -> str:
            st = hedge_states.get(sym)
            return st.value if st is not None else "UNFILLED"
        leg_states = ", ".join(f"{s}={_state_label(s)}" for s in unconfirmed)
        logger.error("UNHEDGED overlay %s: stock %s filled, hedge not confirmed "
                     "FILLED after fill-poll (%s)",
                     plan.overlay.value, stock_req.symbol, leg_states)
        if self._alert_url is None:
            return
        post = self._alert_post or _post_slack
        text = (f"⚠ UNHEDGED — {stock_req.symbol} stock entry filled but the "
                f"intended {plan.overlay.value} hedge is not confirmed FILLED "
                f"(unfilled after the bounded fill-poll). "
                f"Unconfirmed leg(s): {leg_states}. "
                f"Fallback trailing-stop attached.")
        payload = {"text": text}
        try:
            status = post(self._alert_url, payload)
            if not (200 <= status < 300):
                logger.error("UNHEDGED alert POST non-2xx: %s", status)
        except Exception as e:   # never raise into the trading loop
            logger.error("UNHEDGED alert POST failed: %s", e)

    def attach_trailing_stop(self, symbol: str, qty: int, ref_price: float,
                             tag: str) -> bool:
        """Public audited stop-attach (StopManager's morning re-attach). Same
        path as entry-time attachment: risk core -> router -> DB. `tag` seeds
        the client_order_id, so one (tag, symbol, qty) attaches at most once.
        entry_ack stays None here — the position already exists (reconciled),
        so there is nothing to confirm off the book."""
        return self._attach_trailing_stop(symbol, qty, ref_price, tag, entry_ack=None)

    def _attach_trailing_stop(self, symbol: str, qty: int, ref_price: float,
                              entry_signal_id: str, entry_ack=None) -> bool:
        """Place a broker-resting TRAILING_STOP SELL for `qty` shares through the
        SAME audited risk path. Idempotent: the client_order_id is derived from
        the entry's signal id, so re-attaching for the same entry dedupes at the
        router. A rejected stop is logged, never fatal to the entry. Stop
        consolidation on qty changes (pyramiding) is Phase 3. Returns True iff
        the stop was actually placed (risk-approved and submitted).

        C2 fix: on live a MARKET BUY acks SUBMITTED and fills async. Attach
        only after the entry is confirmed off the book, so the re-fetched
        snapshot reflects the position and the risk core approves the stop."""
        if entry_ack is not None and not self._confirm_off_book(entry_ack):
            logger.warning("trailing stop for %s NOT attached — entry %s not "
                           "confirmed filled (intraday stop reconcile is the "
                           "backstop)", symbol, entry_ack.client_order_id)
            return False
        snap = self._b.get_account()  # confirmed filled (or paper auto-fill) -> reflects the position
        cid = OrderRouter.make_client_order_id(symbol, "SELL", qty, f"{entry_signal_id}-stop")
        req = OrderRequest(symbol=symbol, side="SELL", qty=qty, order_type="TRAILING_STOP",
                           limit_price=None, client_order_id=cid,
                           trail_percent=self._cfg.trailing_stop_pct)
        decision = evaluate(req, snap, self._cfg, ref_price=ref_price)
        if not decision.approved:
            logger.warning("trailing stop NOT attached for %s: %s", symbol, decision.reason)
            return False
        ack = self._router.submit(req)
        if self._db:
            self._db.record_trade(
                client_order_id=ack.client_order_id, symbol=req.symbol, side=req.side,
                qty=req.qty, order_type=req.order_type, limit_price=req.limit_price,
                broker_order_id=ack.broker_order_id, state=ack.state.value,
            )
        logger.info("trailing stop attached: %s SELL %d @ %.1f%% trail",
                    symbol, qty, self._cfg.trailing_stop_pct)
        return True

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
        otype, lpx = self._order_kind(trade.side, ref_price)
        req = OrderRequest(symbol=trade.symbol, side=trade.side, qty=trade.qty,
                           order_type=otype, limit_price=lpx,
                           client_order_id=cid)
        decision = evaluate(req, snap, self._cfg, ref_price=ref_price)
        if not decision.approved:
            logger.warning("rebalance rejected %s %s %d: %s", trade.side,
                           trade.symbol, trade.qty, decision.reason)
            return TickResult("REJECTED_BY_RISK", decision.reason)
        ack, term = self._submit_with_escalation(req, snap, ref_price)
        if self._db:
            self._db.record_trade(
                client_order_id=ack.client_order_id, symbol=req.symbol,
                side=req.side, qty=req.qty, order_type=term.order_type,
                limit_price=term.limit_price, broker_order_id=ack.broker_order_id,
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
        the halt flag is set, so the SELLs are not blocked by the halt guard.
        SHARED (V11, C6): a position AutoTrader never traded (the SNP bot's) is
        left alone — flatten only ever touches our own tracked book."""
        from autotrader.rebalance import RebalanceTrade
        owned = None
        if self._cfg.account_ownership == "SHARED" and self._db is not None:
            owned = self._db.owned_symbols()
        for p in snapshot.positions:
            if p.qty <= 0:
                continue
            if owned is not None and p.symbol not in owned:
                logger.info("flatten: foreign position %s left alone (SHARED)", p.symbol)
                continue
            price = self._b.get_quote(p.symbol) or p.avg_price
            trade = RebalanceTrade(p.symbol, "SELL", p.qty, "TRIM", 0)
            res = self.submit_rebalance_order(trade, price, round_id)
            logger.info("flatten %s qty=%d -> %s", p.symbol, p.qty, res.action)

    def cancel_working_orders(self) -> None:
        """SOLE: whole-account cancel (today's behavior). SHARED: cancel only
        our tracked orders — the SNP bot's book is not ours to sweep (C6)."""
        if self._cfg.account_ownership == "SHARED" and self._db is not None:
            from autotrader.lifecycle import cancel_tracked_orders
            cancel_tracked_orders(self._b, self._db)
        else:
            self._b.cancel_all()

    def apply_risk_check(self, now) -> str:
        """Tiered intraday preservation. GATE: close entries, keep positions +
        stops. HALT: flatten all, cancel all, record the halt, set the session
        halt flag. Returns the RiskAction name."""
        snap = self._b.get_account()
        action = risk_evaluate(snap, self._cfg)
        # W7: performance rows are written ONLY by SessionRunner._record_perf
        # (fills-derived realized, quote-based unrealized) — never from engine
        # paths with raw broker figures the EOD report distrusts.
        if action is RiskAction.GATE:
            if self._gate is not None:
                self._gate.close()
            why = "day_pnl UNKNOWN (fail closed)" if not snap.day_pnl_known \
                else f"pnl={snap.day_pnl:.2f}"
            logger.warning("RISK_CHECK soft breach: entries closed (%s)", why)
        elif action is RiskAction.HALT:
            reason = f"daily loss halt: pnl={snap.day_pnl}"
            round_id = f"halt-{now.date().isoformat()}"
            # V4d: cancel FIRST (clears stops/limits), THEN flatten — the
            # liquidation SELLs must never be swept by our own cancel. On live
            # they fill async and must be left resting.
            try:
                self.cancel_working_orders()
            except Exception as e:
                logger.error("RISK_CHECK halt: cancel_all failed: %s", e)
            self._flatten_all(snap, round_id)   # BEFORE halt flag (guard would block)
            if self._gate is not None:
                self._gate.halt()
            if self._db:
                self._db.record_halt(reason)
                self._db.set_state(f"halt:{now.date().isoformat()}", reason)
            logger.error("RISK_CHECK HARD breach: flattened + halted (%s)", reason)
        return action.value

    def shutdown(self) -> None:
        # Cancel-on-shutdown (CLAUDE.md). Best-effort flatten of working orders:
        # a cancel failure is logged, never swallowed silently, and never masks
        # the real shutdown reason.
        try:
            self.cancel_working_orders()
            logger.info("shutdown: cancel_all completed")
        except Exception as e:
            logger.error("shutdown: cancel_all failed: %s", e)
            raise


def build_engine(broker, strategy, cfg, *, order_qty: int, audit_path: str,
                 db=None, entry_gate=None, alert_url=None,
                 strategy_enabled: bool = True, breakout_ref=None) -> TradeEngine:
    """Production TradeEngine wiring — the ONE place real time enters the
    engine: time.sleep for the hedge fill-poll and the escalation dwell (unit
    tests inject no-ops/recorders through the ctor instead), and the snapshot
    cache sized from AUTOTRADER_SNAPSHOT_CACHE_TICKS (default 6 ≈ 30s at the
    5s loop, keeping refresh-token use inside Moomoo's 10-per-30s budget)."""
    import os as _os
    import time as _time
    return TradeEngine(
        broker, strategy, cfg, order_qty=order_qty, audit_path=audit_path,
        db=db, entry_gate=entry_gate, alert_url=alert_url,
        strategy_enabled=strategy_enabled,
        breakout_ref=breakout_ref,
        hedge_confirm_sleep=_time.sleep,
        escalation_sleep=_time.sleep,
        snapshot_cache_ticks=int(_os.getenv("AUTOTRADER_SNAPSHOT_CACHE_TICKS", "6")),
    )


def main() -> int:  # pragma: no cover — live entrypoint, covered by manual run
    import os
    import sys
    import time
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
    from autotrader.strategies.breakout import BreakoutParams, BreakoutStrategy
    from autotrader.breakout_reference import BreakoutReference
    symbol = select_strategy_symbol(cfg.allowed_symbols, os.getenv("STRATEGY_SYMBOL"))
    lookback = int(os.getenv("ENTRY_BREAKOUT_LOOKBACK", "20"))
    strategy_enabled = os.getenv("STRATEGY_ENABLED", "true").strip().lower() in (
        "1", "true", "yes", "on")
    strat = BreakoutStrategy(BreakoutParams(
        symbol=symbol, stop_loss_pct=0.05, take_profit_pct=0.10, confidence=0.7))
    logger.info("internal strategy: BREAKOUT symbol=%s lookback=%d enabled=%s",
                symbol, lookback, strategy_enabled)
    audit = os.path.join(os.path.expanduser("~"), ".futu_trade_audit.jsonl")
    broker = MoomooBroker()
    broker.connect()
    breakout_ref = BreakoutReference(broker, lookback)
    db_path = os.path.expanduser(os.getenv("AUTOTRADER_DB_PATH", "~/.autotrader.db"))
    from autotrader.db import DB  # lazy import: keeps tests that skip main() SDK-free
    db = DB(db_path)
    logger.info("DB projection at %s", db_path)
    from autotrader.clock import Clock
    from autotrader.lifecycle import EntryGate, ground_truth_sync, restore_session_halt
    from autotrader.scheduler import LifecycleScheduler
    from autotrader.watchdog import Watchdog
    from autotrader.runner import SessionRunner

    gate = EntryGate(enabled=False)  # entries open at 09:45 via the scheduler
    restore_session_halt(gate, db, date.today())  # V4c: halted day survives restart
    slack_url = os.getenv("AUTOTRADER_SLACK_WEBHOOK_URL")
    engine = build_engine(broker, strat, cfg,
                          order_qty=int(os.getenv("ORDER_QTY", "1")),
                          audit_path=audit, db=db, entry_gate=gate,
                          alert_url=slack_url,
                          strategy_enabled=strategy_enabled, breakout_ref=breakout_ref)
    owned_only = cfg.account_ownership == "SHARED"
    watchdog = Watchdog(
        health_check=broker.heartbeat,
        reconcile=lambda: ground_truth_sync(broker, db, owned_only=owned_only),
        sleep=time.sleep,
    )
    inbox = None
    inbox_dir = os.getenv("AUTOTRADER_SIGNAL_INBOX")
    if inbox_dir:
        from autotrader.signals.inbox import SignalInbox
        inbox = SignalInbox(os.path.expanduser(inbox_dir),
                            on_targets=db.upsert_target_weights,
                            seen_get=db.get_state, seen_set=db.set_state,
                            ttl_hours=float(os.getenv("AUTOTRADER_SIGNAL_TTL_HOURS", "24")))
        logger.info("external-signal inbox at %s", inbox_dir)
    reporter = None
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

    from autotrader.stops import StopManager
    stop_alert = None
    if slack_url:
        stop_alert = lambda text: _post_slack(slack_url, {"text": text})
    stop_manager = StopManager(engine, broker, db, cfg, alert_fn=stop_alert)

    from autotrader.market_calendar import is_trading_day
    runner = SessionRunner(
        engine=engine, broker=broker, db=db, gate=gate,
        scheduler=LifecycleScheduler(state_get=db.get_state, state_set=db.set_state),
        watchdog=watchdog, clock=Clock(),
        sleep=time.sleep,
        loop_interval=float(os.getenv("AUTOTRADER_LOOP_INTERVAL", "5")),
        signal_inbox=inbox,
        reporter=reporter,
        stop_manager=stop_manager,
        trading_day_fn=lambda d: is_trading_day(d, cfg.market_holidays),
        owned_only=owned_only,
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
