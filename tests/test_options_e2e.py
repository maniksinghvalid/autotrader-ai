from datetime import date, timedelta

from autotrader.config import RiskConfig
from autotrader.domain import OverlayType, Signal
from autotrader.main import TradeEngine
from autotrader.options.chain import OptionQuote
from autotrader.sim_broker import SimBroker
from autotrader.strategies.threshold import ThresholdStrategy, StrategyParams


ASOF = date(2026, 6, 16)


def _chains():
    return {
        ("US.AAPL", "CALL"): [
            OptionQuote("US.AAPL260721C210000", "US.AAPL", ASOF + timedelta(days=35),
                        210, "CALL", 0.30, 1.5)],
        ("US.AAPL", "PUT"): [
            OptionQuote("US.AAPL260721P190000", "US.AAPL", ASOF + timedelta(days=35),
                        190, "PUT", -0.29, 1.4)],
    }


def _cfg(**over):
    base = dict(trading_env="PAPER", min_confidence=0.6, max_order_notional=2000,
                max_position_qty=100, daily_loss_limit=500, max_gross_exposure=50000,
                allowed_symbols=frozenset({"US.AAPL"}), daily_loss_halt=1000,
                allowed_overlays=frozenset({"COVERED_CALL", "PROTECTIVE_PUT"}),
                max_option_contracts=5, max_option_premium_per_trade=800.0)
    base.update(over)
    return RiskConfig(**base)


def _engine(broker, cfg, tmp_path):
    # The overlay tests drive submit_external_signal, not tick(), so the strategy
    # is just a required constructor arg; any valid StrategyParams works.
    strat = ThresholdStrategy(StrategyParams(symbol="US.AAPL", entry_price=1.0,
                                             stop_loss_pct=0.05, take_profit_pct=0.10,
                                             confidence=0.7))
    return TradeEngine(broker, strat, cfg, order_qty=10,
                       audit_path=str(tmp_path / "audit.jsonl"),
                       today_fn=lambda: ASOF)


def _broker(shares=100):
    b = SimBroker(quotes={"US.AAPL": 200.0, "US.AAPL260721C210000": 1.5,
                          "US.AAPL260721P190000": 1.4},
                  cash=1_000_000, option_chains=_chains())
    if shares:
        from autotrader.domain import OrderRequest
        b.place_order(OrderRequest(symbol="US.AAPL", side="BUY", qty=shares,
                                   order_type="MARKET", limit_price=None,
                                   client_order_id="seed"))
    return b


def test_covered_call_places_short_call(tmp_path):
    b = _broker(shares=200)
    eng = _engine(b, _cfg(), tmp_path)
    res = eng.submit_external_signal(
        Signal("US.AAPL", "SELL", 0.7, "Covered Call", overlay=OverlayType.COVERED_CALL))
    assert res.action == "OVERLAY_PLACED", res
    acc = b.get_account()
    held = {p.symbol: p.qty for p in acc.positions}
    assert held["US.AAPL260721C210000"] == -2   # short 2 calls vs 200 shares


def test_protective_put_places_long_put(tmp_path):
    b = _broker(shares=100)
    eng = _engine(b, _cfg(), tmp_path)
    res = eng.submit_external_signal(
        Signal("US.AAPL", "SELL", 0.7, "Protective Put", overlay=OverlayType.PROTECTIVE_PUT))
    assert res.action == "OVERLAY_PLACED", res
    held = {p.symbol: p.qty for p in b.get_account().positions}
    assert held["US.AAPL260721P190000"] == 1


def test_overlay_skipped_no_underlying(tmp_path):
    b = _broker(shares=0)
    eng = _engine(b, _cfg(), tmp_path)
    res = eng.submit_external_signal(
        Signal("US.AAPL", "SELL", 0.7, "Covered Call", overlay=OverlayType.COVERED_CALL))
    assert res.action == "SKIP_NO_UNDERLYING", res


def test_overlay_low_confidence_dropped(tmp_path):
    b = _broker(shares=200)
    eng = _engine(b, _cfg(), tmp_path)
    res = eng.submit_external_signal(
        Signal("US.AAPL", "SELL", 0.3, "Covered Call", overlay=OverlayType.COVERED_CALL))
    assert res.action == "DROPPED_LOW_CONFIDENCE", res


def test_equity_signal_unaffected(tmp_path):
    b = _broker(shares=0)
    eng = _engine(b, _cfg(), tmp_path)
    res = eng.submit_external_signal(Signal("US.AAPL", "BUY", 0.7, "plain"))
    assert res.action == "ORDER_PLACED", res


# ---------------------------------------------------------------------------
# I1b — overlay OPEN legs blocked when entry gate is closed
# ---------------------------------------------------------------------------

class _ClosedGate:
    """Minimal stub: gate is closed (entries_enabled=False) and not halted."""
    entries_enabled = False
    halted = False


def test_covered_call_blocked_when_entry_gate_closed(tmp_path):
    """A covered-call OPEN signal is blocked when the entry window is closed,
    even though shares are held — mirroring the BUY-entry gate for equity."""
    b = _broker(shares=200)
    strat = ThresholdStrategy(StrategyParams(symbol="US.AAPL", entry_price=1.0,
                                             stop_loss_pct=0.05, take_profit_pct=0.10,
                                             confidence=0.7))
    eng = TradeEngine(b, strat, _cfg(), order_qty=10,
                      audit_path=str(tmp_path / "audit.jsonl"),
                      today_fn=lambda: ASOF,
                      entry_gate=_ClosedGate())
    res = eng.submit_external_signal(
        Signal("US.AAPL", "SELL", 0.7, "Covered Call", overlay=OverlayType.COVERED_CALL))
    assert res.action == "ENTRY_CLOSED", res


# ---------------------------------------------------------------------------
# Task 8 — multi-leg overlays: coverage wiring + loud long-only residual
# ---------------------------------------------------------------------------

def _rich_chains():
    near, far = ASOF + timedelta(days=35), ASOF + timedelta(days=300)
    return {
        ("US.AAPL", "CALL"): [
            OptionQuote("US.AAPL270412C180000", "US.AAPL", far, 180, "CALL", 0.80, 30.0),
            OptionQuote("US.AAPL270412C195000", "US.AAPL", far, 195, "CALL", 0.70, 20.0),
            OptionQuote("US.AAPL260721C210000", "US.AAPL", near, 210, "CALL", 0.30, 1.5),
        ],
        ("US.AAPL", "PUT"): [
            OptionQuote("US.AAPL260721P200000", "US.AAPL", near, 200, "PUT", -0.45, 4.0),
            OptionQuote("US.AAPL260721P190000", "US.AAPL", near, 190, "PUT", -0.30, 2.0),
            OptionQuote("US.AAPL260721P185000", "US.AAPL", near, 185, "PUT", -0.22, 1.5),
        ],
    }


def _rich_broker(shares=0):
    b = SimBroker(quotes={
        "US.AAPL": 200.0,
        "US.AAPL270412C180000": 30.0, "US.AAPL270412C195000": 20.0,
        "US.AAPL260721C210000": 1.5,
        "US.AAPL260721P200000": 4.0, "US.AAPL260721P190000": 2.0,
        "US.AAPL260721P185000": 1.5,
    }, cash=1_000_000, option_chains=_rich_chains())
    if shares:
        from autotrader.domain import OrderRequest
        b.place_order(OrderRequest(symbol="US.AAPL", side="BUY", qty=shares,
                                   order_type="MARKET", limit_price=None,
                                   client_order_id="seed"))
    return b


def _cfgN(**over):
    return _cfg(allowed_overlays=frozenset({
        "COVERED_CALL", "PROTECTIVE_PUT", "COLLAR",
        "BEAR_PUT_SPREAD", "CALL_DIAGONAL", "LEAP"}),
        max_option_premium_per_trade=5000.0, option_default_contracts=1, **over)


def test_bear_put_spread_places_both_legs(tmp_path):
    b = _rich_broker(shares=0)
    eng = _engine(b, _cfgN(), tmp_path)
    res = eng.submit_external_signal(
        Signal("US.AAPL", "SELL", 0.7, "bear put", overlay=OverlayType.BEAR_PUT_SPREAD))
    assert res.action == "OVERLAY_PLACED", res
    held = {p.symbol: p.qty for p in b.get_account().positions}
    assert held["US.AAPL260721P200000"] == 1    # long higher-strike put
    assert held["US.AAPL260721P185000"] == -1   # short lower-strike put


def test_call_diagonal_places_both_legs(tmp_path):
    b = _rich_broker(shares=0)
    eng = _engine(b, _cfgN(), tmp_path)
    res = eng.submit_external_signal(
        Signal("US.AAPL", "SELL", 0.7, "pmcc", overlay=OverlayType.CALL_DIAGONAL))
    assert res.action == "OVERLAY_PLACED", res
    held = {p.symbol: p.qty for p in b.get_account().positions}
    assert held["US.AAPL270412C180000"] == 1    # long LEAP call
    assert held["US.AAPL260721C210000"] == -1   # short near call


def test_leap_places_single_long_call(tmp_path):
    b = _rich_broker(shares=0)
    eng = _engine(b, _cfgN(), tmp_path)
    res = eng.submit_external_signal(
        Signal("US.AAPL", "SELL", 0.7, "leap", overlay=OverlayType.LEAP))
    assert res.action == "OVERLAY_PLACED", res
    held = {p.symbol: p.qty for p in b.get_account().positions}
    assert held["US.AAPL270412C180000"] == 1


def test_collar_places_long_put_and_short_call(tmp_path):
    b = _rich_broker(shares=100)
    eng = _engine(b, _cfgN(), tmp_path)
    res = eng.submit_external_signal(
        Signal("US.AAPL", "SELL", 0.7, "collar", overlay=OverlayType.COLLAR))
    assert res.action == "OVERLAY_PLACED", res
    held = {p.symbol: p.qty for p in b.get_account().positions}
    assert held["US.AAPL260721P190000"] == 1    # long put
    assert held["US.AAPL260721C210000"] == -1   # short call (share-covered)


class _RejectShortBroker(SimBroker):
    """Fills the long (BUY) leg, but REJECTS any short OPEN option leg — to
    exercise the long-only safe residual path."""
    def place_order(self, req):
        if req.option is not None and req.side == "SELL" and req.position_effect == "OPEN":
            self._seq += 1
            from autotrader.domain import OrderAck, OrderState
            return OrderAck(req.client_order_id, f"sim-{self._seq}", OrderState.REJECTED, {})
        return super().place_order(req)


def test_spread_short_leg_rejected_leaves_loud_long_residual(tmp_path):
    b = _RejectShortBroker(quotes={
        "US.AAPL": 200.0, "US.AAPL260721P200000": 4.0, "US.AAPL260721P185000": 1.5,
    }, cash=1_000_000, option_chains=_rich_chains())
    eng = _engine(b, _cfgN(), tmp_path)
    res = eng.submit_external_signal(
        Signal("US.AAPL", "SELL", 0.7, "bear put", overlay=OverlayType.BEAR_PUT_SPREAD))
    assert res.action == "OVERLAY_RESIDUAL_LONG", res
    held = {p.symbol: p.qty for p in b.get_account().positions}
    assert held["US.AAPL260721P200000"] == 1            # long put filled
    assert "US.AAPL260721P185000" not in held           # short put never opened


# ---------------------------------------------------------------------------
# §2 Task 5 — engine pre-checks the hedge group before the stock leg;
# no naked entry (§2.A)
# ---------------------------------------------------------------------------

from autotrader.domain import OrderAck, OrderState, OrderRequest


class _RejectHedgeBroker(SimBroker):
    """Fills stock (option=None) but REJECTS any OPEN option leg — models a
    hedge that cannot be placed."""
    def place_order(self, req):
        if req.option is not None and req.position_effect == "OPEN":
            self._seq += 1
            return OrderAck(req.client_order_id, f"sim-{self._seq}",
                            OrderState.REJECTED, {})
        return super().place_order(req)


def _pp_entry(monkeypatch):
    from autotrader.options import overlays
    from autotrader.options.overlays import OverlayDef, LegSpec
    monkeypatch.setitem(
        overlays.REGISTRY, OverlayType.PROTECTIVE_PUT,
        OverlayDef(requires_underlying=False, opens_stock=True,
                   legs=(LegSpec(right="PUT", side="BUY"),)))


def test_hedge_unplaceable_blocks_stock_entry_no_naked(tmp_path, monkeypatch):
    _pp_entry(monkeypatch)
    b = _RejectHedgeBroker(
        quotes={"US.AAPL": 200.0, "US.AAPL260721P190000": 1.4},
        cash=1_000_000, option_chains=_chains())
    eng = _engine(b, _cfg(allowed_overlays=frozenset({"PROTECTIVE_PUT"}),
                          max_order_notional=25_000), tmp_path)
    res = eng.submit_external_signal(
        Signal("US.AAPL", "BUY", 0.7, "Protective Put",
               overlay=OverlayType.PROTECTIVE_PUT))
    # A rejected hedge in a plan with a stock leg -> pre-check fails,
    # NOTHING placed. No naked stock.
    assert res.action == "OVERLAY_HEDGE_UNPLACEABLE", res
    held = {p.symbol: p.qty for p in b.get_account().positions}
    assert held.get("US.AAPL", 0) == 0        # stock NEVER bought
    assert "US.AAPL260721P190000" not in held


def test_hedge_placeable_then_stock_entry_places_both(tmp_path, monkeypatch):
    _pp_entry(monkeypatch)
    b = SimBroker(quotes={"US.AAPL": 200.0, "US.AAPL260721P190000": 1.4},
                  cash=1_000_000, option_chains=_chains())
    eng = _engine(b, _cfg(allowed_overlays=frozenset({"PROTECTIVE_PUT"}),
                          max_order_notional=25_000), tmp_path)
    res = eng.submit_external_signal(
        Signal("US.AAPL", "BUY", 0.7, "Protective Put",
               overlay=OverlayType.PROTECTIVE_PUT))
    assert res.action == "OVERLAY_PLACED", res
    held = {p.symbol: p.qty for p in b.get_account().positions}
    assert held["US.AAPL"] == 100                 # stock bought after hedge
    assert held["US.AAPL260721P190000"] == 1      # protective put filled


# ---------------------------------------------------------------------------
# fallback trailing-stop + immediate Slack UNHEDGED alert (§2.B)
# ---------------------------------------------------------------------------


class _RestingHedgeBroker(SimBroker):
    """Fills stock (option=None) but leaves any OPEN option leg RESTING
    (SUBMITTED, never FILLED) — models a venue that does not fill options.
    The order stays in _open across every get_open_orders() poll, so the
    bounded fill-poll in _confirm_hedge_fill exhausts and the hedge is
    unconfirmed."""
    def place_order(self, req):
        if req.option is not None and req.position_effect == "OPEN":
            if req.client_order_id in self._acks_by_cid:
                return self._acks_by_cid[req.client_order_id]
            self._seq += 1
            boid = f"sim-{self._seq}"
            ack = OrderAck(req.client_order_id, boid, OrderState.SUBMITTED, {})
            self._open[boid] = ack
            self._acks_by_cid[req.client_order_id] = ack
            return ack
        return super().place_order(req)


class _AsyncFillHedgeBroker(SimBroker):
    """Models a LIVE venue: the OPEN option leg acks SUBMITTED at placement
    and then FILLS asynchronously — it is present in the FIRST get_open_orders()
    poll and absent thereafter (the bounded fill-poll observes it leave the
    book). The stock leg (option=None) fills immediately via super()."""
    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self._opt_polls = 0

    def place_order(self, req):
        if req.option is not None and req.position_effect == "OPEN":
            if req.client_order_id in self._acks_by_cid:
                return self._acks_by_cid[req.client_order_id]
            self._seq += 1
            boid = f"sim-{self._seq}"
            ack = OrderAck(req.client_order_id, boid, OrderState.SUBMITTED, {})
            self._open[boid] = ack           # present in the first poll
            self._acks_by_cid[req.client_order_id] = ack
            return ack
        return super().place_order(req)

    def get_open_orders(self):
        # First poll shows the resting option order; the next poll shows it
        # filled (gone from the book) — an async live fill within the window.
        orders = super().get_open_orders()
        self._opt_polls += 1
        if self._opt_polls >= 2:
            # after the first poll, the SUBMITTED option order "filled" and
            # left the book (async live fill); a resting -stop, if any, stays.
            self._open = {b: a for b, a in self._open.items()
                          if a.state is not OrderState.SUBMITTED}
            return list(self._open.values())
        return orders


def test_stock_filled_hedge_unconfirmed_attaches_stop_and_alerts(tmp_path, monkeypatch):
    _pp_entry(monkeypatch)
    b = _RestingHedgeBroker(
        quotes={"US.AAPL": 200.0, "US.AAPL260721P190000": 1.4},
        cash=1_000_000, option_chains=_chains())
    alerts = []
    strat = ThresholdStrategy(StrategyParams(symbol="US.AAPL", entry_price=1.0,
                                             stop_loss_pct=0.05, take_profit_pct=0.10,
                                             confidence=0.7))
    eng = TradeEngine(
        b, strat, _cfg(allowed_overlays=frozenset({"PROTECTIVE_PUT"}),
                       trailing_stop_pct=5.0, max_order_notional=25_000),
        order_qty=10, audit_path=str(tmp_path / "audit.jsonl"),
        today_fn=lambda: ASOF,
        hedge_confirm_attempts=2, hedge_confirm_sleep=lambda _s: None,
        alert_url="https://hooks.slack.test/x",
        alert_post=lambda url, payload: (alerts.append(payload) or 200))
    res = eng.submit_external_signal(
        Signal("US.AAPL", "BUY", 0.7, "Protective Put",
               overlay=OverlayType.PROTECTIVE_PUT))
    assert res.action == "OVERLAY_PLACED", res
    held = {p.symbol: p.qty for p in b.get_account().positions}
    assert held["US.AAPL"] == 100                     # stock filled
    # Two orders now REST: the never-filling hedge AND the protective trailing
    # stop attached as the fallback (SimBroker rests every TRAILING_STOP). Both
    # are OrderAcks; make_client_order_id hashes cids (at-<sha1>), so we count
    # rather than grep the cid. A TRAILING_STOP fill-fallback was added -> +1.
    open_after = b.get_open_orders()
    assert len(open_after) == 2                        # resting hedge + fallback stop
    # exactly one UNHEDGED alert fired, naming symbol/overlay/reason
    assert len(alerts) == 1
    txt = alerts[0]["text"]
    assert "UNHEDGED" in txt
    assert "US.AAPL" in txt
    assert "PROTECTIVE_PUT" in txt
    # the reason: hedge not confirmed FILLED after the bounded fill-poll
    # (state-agnostic token; also holds for a REJECTED hedge)
    assert "not confirmed" in txt.lower() or "unfilled" in txt.lower()


def test_stock_filled_hedge_async_fills_within_poll_no_alert(tmp_path, monkeypatch):
    # LIVE model: hedge acks SUBMITTED then fills on the next fill-poll ->
    # _confirm_hedge_fill returns True -> hedge CONFIRMED -> NO alert, NO
    # fallback stop (only the resting hedge could be open; assert no -stop order).
    _pp_entry(monkeypatch)
    b = _AsyncFillHedgeBroker(
        quotes={"US.AAPL": 200.0, "US.AAPL260721P190000": 1.4},
        cash=1_000_000, option_chains=_chains())
    alerts = []
    strat = ThresholdStrategy(StrategyParams(symbol="US.AAPL", entry_price=1.0,
                                             stop_loss_pct=0.05, take_profit_pct=0.10,
                                             confidence=0.7))
    eng = TradeEngine(
        b, strat, _cfg(allowed_overlays=frozenset({"PROTECTIVE_PUT"}),
                       trailing_stop_pct=5.0, max_order_notional=25_000),
        order_qty=10, audit_path=str(tmp_path / "audit.jsonl"),
        today_fn=lambda: ASOF,
        hedge_confirm_attempts=3, hedge_confirm_sleep=lambda _s: None,
        alert_url="https://hooks.slack.test/x",
        alert_post=lambda url, payload: (alerts.append(payload) or 200))
    res = eng.submit_external_signal(
        Signal("US.AAPL", "BUY", 0.7, "Protective Put",
               overlay=OverlayType.PROTECTIVE_PUT))
    assert res.action == "OVERLAY_PLACED", res
    assert alerts == []              # hedge async-filled within the poll -> confirmed
    # No fallback trailing stop was attached: the hedge left the book on the
    # async poll and no other order rests. (The async broker drops filled
    # SUBMITTED orders; a fallback TRAILING_STOP, had one been added, would rest
    # and show here.)
    assert b.get_open_orders() == []


def test_no_alert_when_hedge_confirmed(tmp_path, monkeypatch):
    _pp_entry(monkeypatch)
    b = SimBroker(quotes={"US.AAPL": 200.0, "US.AAPL260721P190000": 1.4},
                  cash=1_000_000, option_chains=_chains())
    alerts = []
    strat = ThresholdStrategy(StrategyParams(symbol="US.AAPL", entry_price=1.0,
                                             stop_loss_pct=0.05, take_profit_pct=0.10,
                                             confidence=0.7))
    eng = TradeEngine(
        b, strat, _cfg(allowed_overlays=frozenset({"PROTECTIVE_PUT"}), max_order_notional=25_000),
        order_qty=10, audit_path=str(tmp_path / "audit.jsonl"),
        today_fn=lambda: ASOF,
        alert_url="https://hooks.slack.test/x",
        alert_post=lambda url, payload: (alerts.append(payload) or 200))
    res = eng.submit_external_signal(
        Signal("US.AAPL", "BUY", 0.7, "Protective Put",
               overlay=OverlayType.PROTECTIVE_PUT))
    assert res.action == "OVERLAY_PLACED", res
    assert alerts == []              # hedge FILLED -> no alert
