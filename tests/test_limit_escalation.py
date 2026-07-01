import pytest
from autotrader.config import RiskConfig
from autotrader.domain import OrderRequest, OrderState, Position
from autotrader.risk_core import evaluate as real_evaluate
from autotrader.sim_broker import SimBroker
from autotrader.strategies.threshold import ThresholdStrategy, StrategyParams
from autotrader import main as main_mod
from autotrader.main import TradeEngine


def _cfg(**over):
    base = dict(trading_env="PAPER", min_confidence=0.6, max_order_notional=1_000_000,
                max_position_qty=10_000, daily_loss_limit=500, max_gross_exposure=100_000_000,
                allowed_symbols=frozenset({"US.AAPL"}),
                limit_orders_enabled=True, order_cap_bps=5.0)
    base.update(over)
    return RiskConfig(**base)


def _engine(broker, cfg, tmp_path):
    strat = ThresholdStrategy(StrategyParams(symbol="US.AAPL", entry_price=100.0,
                                             stop_loss_pct=0.05, take_profit_pct=0.10,
                                             confidence=0.7))
    return TradeEngine(broker=broker, strategy=strat, cfg=cfg,
                       order_qty=10, audit_path=str(tmp_path / "audit.jsonl"))


def test_marketable_limit_fills_without_escalation(tmp_path):
    # 50 bps cap, 10 bps spread -> marketable -> fills on the first submit, no market fallback.
    b = SimBroker(quotes={"US.AAPL": 100.0}, cash=1_000_000.0, spread_bps=10.0)
    eng = _engine(b, _cfg(order_cap_bps=50.0), tmp_path)
    assert eng.tick().action == "ORDER_PLACED"
    assert b.get_account().position_qty("US.AAPL") == 10
    assert b.get_open_orders() == []                 # nothing left resting


def test_unfilled_limit_escalates_to_market_and_completes(tmp_path):
    # 5 bps cap but 100 bps spread -> limit NOT marketable, rests; re-peg still not
    # marketable; MARKET fallback completes the fill (a risk exit must complete).
    b = SimBroker(quotes={"US.AAPL": 100.0}, cash=1_000_000.0, spread_bps=100.0)
    eng = _engine(b, _cfg(order_cap_bps=5.0), tmp_path)
    assert eng.tick().action == "ORDER_PLACED"
    assert b.get_account().position_qty("US.AAPL") == 10   # filled via MARKET fallback
    # MARKET buy paid the ask (100 * (1 + 0.01)) = 101.0.
    assert b.reconcile_fills(None)[-1].price == pytest.approx(101.0)
    assert b.get_open_orders() == []                       # resting limits cancelled


def test_escalation_records_terminal_state_only(tmp_path):
    import json
    b = SimBroker(quotes={"US.AAPL": 100.0}, cash=1_000_000.0, spread_bps=100.0)
    eng = _engine(b, _cfg(order_cap_bps=5.0), tmp_path)
    eng.tick()
    with open(tmp_path / "audit.jsonl") as f:
        acks = [json.loads(l) for l in f if json.loads(l)["action"] == "ack"]
    # last ack is the filled MARKET fallback.
    assert acks[-1]["state"] == OrderState.FILLED.value


def test_risk_vetoed_market_fallback_does_not_bypass_risk(tmp_path, monkeypatch):
    """T6b (Minor #4): when the MARKET fallback (escalation stage 3) is rejected
    by the risk core, _submit_with_escalation must NOT submit the MARKET order
    and must return the prior resting-limit ack — execution can never bypass risk.

    Chosen "risk lever" and why a pure _cfg(...) override cannot drive this branch:
    risk_core._notional() prices a LIMIT at its (capped) limit_price and a MARKET
    at ref_price. capped_limit_price() adds a non-negative cap to a BUY's ref
    price (ref+cap) and subtracts it from a SELL's (ref-cap), so for a BUY the
    peg's evaluated notional is ALWAYS >= the MARKET fallback's (same ref, cap>=0)
    — max_order_notional/max_gross_exposure can only reject the peg before/instead
    of MARKET, never MARKET-while-peg-passes. For a SELL, risk_core's
    is_reduce_only exemption (side=="SELL" and 0<=resulting<held) is TRUE for
    every risk-approved SELL (qty>0 flattening a long always satisfies
    0<=resulting<held), so notional/exposure checks are skipped entirely and can
    never reject a valid SELL's MARKET stage either. max_position_qty/day_pnl
    likewise can't discriminate: qty/side/held are identical across the LIMIT,
    peg, and MARKET requests, so those checks pass or fail identically for all
    three. This was verified empirically (see fix report) — no _cfg(...)
    combination can make risk_core.evaluate() approve the capped LIMIT/peg while
    rejecting the MARKET fallback for the same order.
    Per the fix brief's explicit escape hatch ("if you cannot construct a valid
    risk-veto scenario without touching a config default... do not fake it"),
    this test instead uses a test-local monkeypatch of autotrader.main.evaluate
    that DELEGATES to the real risk_core.evaluate for every request except the
    MARKET fallback (order_type == "MARKET" and limit_price is None), which it
    forces to reject. This keeps the LIMIT + re-peg stages on the real risk core
    (they pass on their own merits — wide spread just makes them rest) and
    isolates exactly the branch under test: MARKET rejected by risk.
    """
    import json

    def fake_evaluate(req, snap, cfg, ref_price=None, coverage_legs=()):
        if req.order_type == "MARKET" and req.limit_price is None:
            from autotrader.risk_core import RiskDecision
            return RiskDecision(False, "test-forced: MARKET fallback vetoed by risk")
        return real_evaluate(req, snap, cfg, ref_price=ref_price, coverage_legs=coverage_legs)

    monkeypatch.setattr(main_mod, "evaluate", fake_evaluate)

    # Same wide-spread/tight-cap setup as test_unfilled_limit_escalates_to_market_
    # and_completes: capped LIMIT is non-marketable, rests; re-peg also rests
    # (touch doesn't move), forcing escalation to stage 3.
    b = SimBroker(quotes={"US.AAPL": 100.0}, cash=1_000_000.0, spread_bps=100.0)
    eng = _engine(b, _cfg(order_cap_bps=5.0), tmp_path)

    result = eng.tick()

    # No position opened — the vetoed MARKET fallback never reached the broker.
    assert b.get_account().position_qty("US.AAPL") == 0
    # No fill was recorded at all (LIMIT/peg rested; MARKET was never submitted).
    assert b.reconcile_fills(None) == []
    # The resting re-peg limit was cancelled ahead of the (vetoed) MARKET attempt
    # (existing stage-3 cancel-then-fallback control flow, unchanged by this fix),
    # so nothing is left open — and critically no "-mkt" order was ever submitted.
    open_cids = {o.client_order_id for o in b.get_open_orders()}
    assert open_cids == set()
    assert not any(cid.endswith("-mkt") for cid in open_cids)

    # Audit journal: NO ack with a "-mkt" client_order_id was ever submitted.
    with open(tmp_path / "audit.jsonl") as f:
        lines = [json.loads(l) for l in f]
    mkt_acks = [l for l in lines if l["action"] == "ack"
                and l["client_order_id"].endswith("-mkt")]
    assert mkt_acks == []
    # The last recorded ack is the resting peg, still SUBMITTED (not FILLED).
    acks = [l for l in lines if l["action"] == "ack"]
    assert acks[-1]["client_order_id"].endswith("-peg")
    assert acks[-1]["state"] == OrderState.SUBMITTED.value

    assert result.action == "ORDER_PLACED"
