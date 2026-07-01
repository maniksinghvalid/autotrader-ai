# Protective Limit Orders Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to execute this plan. Each task below is a self-contained TDD unit — write the failing test, run it (confirm the expected FAIL), implement the minimum code, run it (confirm PASS), then commit. Do not batch tasks. Do not skip the RED step.

**Goal:** Replace naked MARKET orders with price-capped **marketable limit** orders that still guarantee execution on risk exits (via re-peg-then-market escalation), and give the paper `SimBroker` a **spread/slippage model** so the execution-quality improvement is measurable in paper. Ship the slippage model first (the measuring stick), then the capped-limit construction, then escalation, all behind a config flag that defaults to **current MARKET behavior**.

**Architecture:** The change is confined to the domain/execution seam that already exists — no new package. `OrderRequest.limit_price` and the broker `LIMIT → OrderType.NORMAL` plumbing (`moomoo_broker.py:212-213`) are reused as-is; nothing new is added to the SDK adapter's order path. A new pure helper module `autotrader/limit_pricing.py` computes a capped marketable limit from a reference price + touch, driven entirely by config. `SimBroker` grows a deterministic simulated bid/ask around its stored reference so MARKET pays the spread and LIMIT fills only when marketable — no clock, no randomness (fill ids stay counters). The three submission points in `main.py` (strategy signals, rebalance) and `options/planner.py` (option legs) consult the helper only when `RISK_LIMIT_ORDERS_ENABLED` is true; when false they emit the exact `order_type="MARKET", limit_price=None` requests they emit today. Escalation (re-peg-then-market) is a bounded loop in a new `TradeEngine` helper that reuses the existing `OrderRouter.submit`, `broker.get_open_orders`, and `broker.cancel_order`. Trailing-stop exits are untouched.

**Tech Stack:** Python 3, stdlib only (`dataclasses`, `math`, `enum`), `pytest`. No new third-party dependencies. All tests run offline (no OpenD, no `moomoo` SDK) against `SimBroker` and pure helpers.

### Investigation findings baked into this plan (spec Open Items 2 & 3)

Task 0 is a code-reading investigation; its conclusions (already gathered while drafting and re-verified in Task 0) shape every later task:

- **Open Item 2 — true bid/ask vs last/mid.** The vendored SDK snapshot **does** expose `bid_price` / `ask_price` (`skills/moomooapi/scripts/quote/get_snapshot.py:57-58` maps `bid_price`→`bid`, `ask_price`→`ask`; `get_option_chain` in `moomoo_broker.py:184-185` already reads `bid_price`/`ask_price`). **But the app's `MoomooBroker.get_quote()` currently reads only `last_price` (`moomoo_broker.py:106`)** and returns a single float; the `Broker.get_quote` contract is `Optional[float]`. Decision for this plan: **keep the cap applied around a single reference price** (the value `get_quote` returns — `last` today) rather than widening the `Broker.get_quote` contract to return a bid/ask pair. The `SimBroker` slippage model *derives* a simulated bid/ask from that reference for paper realism; the cap helper takes `ref_price` + `side` and applies `cap` symmetrically (BUY caps above ref, SELL caps below ref). Plumbing true live bid/ask into the cap is a documented follow-up, out of scope here. This keeps the `Broker` interface stable and both brokers on one code path.
- **Open Item 3 — per-order modify/cancel for escalation.** `MoomooBroker.cancel_order` exists and cancels via `modify_order(ModifyOrderOp.CANCEL, ...)` (`moomoo_broker.py:224-231`); `get_open_orders()` returns working `OrderAck`s keyed by the client_order_id-in-remark with a neutral `OrderState` (`moomoo_broker.py:243-259`); `SimBroker.cancel_order`/`get_open_orders` mirror this. There is **no** per-order "get one order status" call, so the escalation checks fill status by testing whether the order's `client_order_id` still appears in `get_open_orders()` (present ⇒ still working/unfilled; absent ⇒ terminal, treated as filled/done). This uses only methods already on the `Broker` base class — no new broker method is required.

## Global Constraints

Copied verbatim from `CLAUDE.md` (the rules that govern this change):

- **Default is paper trading (`TrdEnv.SIMULATE`).** Never infer live-trading intent from conversation context. Live requires explicit `TRADING_ENV=LIVE` **and** manual GUI unlock. This plan is paper-only; every test runs against `SimBroker` / `TrdEnv.SIMULATE`.
- **Never call `unlock_trade` / `TrdUnlockTrade` / `trd_unlock_trade` via the SDK.** No code in this plan touches trade-unlock.
- Risk-limit **values** require explicit human review to change — **do not modify them as part of an unrelated task.** This plan **adds** three new risk params (`RISK_ORDER_CAP_BPS`, `RISK_ORDER_CAP_TICKS`, `RISK_LIMIT_ORDERS_ENABLED`) to `config/`; it does **not** change any existing risk-limit value.
- **Hardcode API keys, hosts, ports, or risk parameters anywhere outside `config/` — Never.** The cap bps, cap ticks, and enable flag live in `RiskConfig` and are read from `RISK_*` env only.
- **Check `ret_code == RET_OK` on every Moomoo call; log and re-raise on failure.** No change to the SDK order path is introduced that bypasses this; the existing `_ok(ret)` checks stay.
- **Catch exceptions explicitly, log with context, recover or re-raise. No bare `except: pass`.** Escalation catches `BrokerError` around cancel and logs it.
- **Do not modify files under `skills/install-moomoo-opend/` or `skills/moomooapi/`.** Vendored Futu code is read-only reference; this plan reads it (Task 0) but never edits it.
- **All new tests are offline** — no live OpenD, no `moomoo` SDK import in core (`tests/test_no_sdk_in_core.py` must still pass).
- **Trailing-stop exits stay broker-resting/unchanged** (`main.py:246`, `_attach_trailing_stop`, `consolidate_stop`).

## File Structure

| File | Responsibility | Change |
|---|---|---|
| `autotrader/config.py` | `RiskConfig` dataclass + `load_risk_config()` env loader | **Edit** — add `order_cap_bps`, `order_cap_ticks`, `limit_orders_enabled` fields (default off) + `RISK_*` loading |
| `autotrader/limit_pricing.py` | Pure helper: compute capped marketable limit from ref price + side + cfg; tick-size ladder | **New** |
| `autotrader/sim_broker.py` | In-memory paper broker | **Edit** — simulated bid/ask spread model; MARKET fills far touch (+slippage), LIMIT fills only if marketable, else rests |
| `autotrader/main.py` | `TradeEngine`: strategy-signal + rebalance submission; new escalation helper | **Edit** — build capped limit at signal/rebalance points when flag on; add `_submit_with_escalation` |
| `autotrader/options/planner.py` | Overlay leg `OrderRequest` construction | **Edit** — build capped limit for option legs when flag on (premium as ref) |
| `tests/test_limit_pricing.py` | Cap math incl. tick floor on low-priced names | **New** |
| `tests/test_sim_broker_slippage.py` | Spread model: MARKET pays spread, marketable LIMIT fills at cap, non-marketable LIMIT rests | **New** |
| `tests/test_limit_order_wiring.py` | Flag-on wiring at signals/rebalance/option legs; **flag-off = byte-for-byte current MARKET** | **New** |
| `tests/test_limit_escalation.py` | unfilled → re-peg → market escalation; risk-exit always completes | **New** |
| `tests/test_config.py` | Config loader tests | **Edit** — assert new keys default off + parse |

---

### Task 0: Investigation — confirm bid/ask exposure and modify/cancel support (spec Open Items 2 & 3)

**Files:** Read-only. `autotrader/moomoo_broker.py` (`get_quote` ~102-106; `get_option_chain` bid/ask ~184-185; `place_order` order-type map 206-213; `cancel_order` 224-231; `get_open_orders` 243-259); `autotrader/broker.py` (contract); `autotrader/sim_broker.py` (46-71); `skills/moomooapi/scripts/quote/get_snapshot.py` (57-58) and `get_orderbook.py` (read only — do NOT edit `skills/`).

**Interfaces:**
- Consumes: existing source only.
- Produces: a short written finding recorded as the "Investigation findings" note at the top of this plan (already captured) — no code.

- [ ] Read `MoomooBroker.get_quote` and confirm it returns a single `Optional[float]` sourced from `last_price` only (line 106). Confirm the `Broker.get_quote` base contract is `Optional[float]`.
- [ ] Read `skills/moomooapi/scripts/quote/get_snapshot.py` lines 50-58 and `MoomooBroker.get_option_chain` lines 184-185; confirm `bid_price` / `ask_price` are available from `get_market_snapshot` at the SDK level (they are).
- [ ] Conclude and record: **true bid/ask exists at the SDK level but is not plumbed through `get_quote`; this plan applies the cap around the single `ref_price` returned by `get_quote` and derives a simulated bid/ask only inside `SimBroker`.** (Matches the "Investigation findings" note above.)
- [ ] Read `MoomooBroker.cancel_order` (224-231) and `get_open_orders` (243-259); confirm per-order cancel exists and that fill status is observable only by absence from `get_open_orders()` (no single-order status call). Confirm `SimBroker.cancel_order`/`get_open_orders` mirror this.
- [ ] Conclude and record: **escalation will poll `get_open_orders()` and treat "client_order_id no longer working" as filled/terminal; cancel via `broker.cancel_order(broker_order_id)`.** No new `Broker` method required.
- [ ] No commit (investigation task, no code change). Proceed to Task 1.

---

### Task 1: Config keys — `RISK_ORDER_CAP_BPS`, `RISK_ORDER_CAP_TICKS`, `RISK_LIMIT_ORDERS_ENABLED` (§3.D)

**Files:** `autotrader/config.py` (add fields after `option_max_risk_pct` ~56; add loading in `load_risk_config` ~118); Test: `tests/test_config.py`.

**Interfaces:**
- Consumes: env vars `RISK_ORDER_CAP_BPS` (float, default `0.0`), `RISK_ORDER_CAP_TICKS` (float, default `0.0`), `RISK_LIMIT_ORDERS_ENABLED` (bool, default `False`).
- Produces: `RiskConfig` fields `order_cap_bps: float = 0.0`, `order_cap_ticks: float = 0.0`, `limit_orders_enabled: bool = False`.

- [ ] Add the RED test to `tests/test_config.py`:
```python
def test_loader_reads_limit_order_knobs_with_safe_defaults(monkeypatch):
    for k in ("RISK_ORDER_CAP_BPS", "RISK_ORDER_CAP_TICKS", "RISK_LIMIT_ORDERS_ENABLED"):
        monkeypatch.delenv(k, raising=False)
    cfg = load_risk_config()
    # DEFAULT OFF -> current MARKET behavior preserved.
    assert cfg.limit_orders_enabled is False
    assert cfg.order_cap_bps == 0.0
    assert cfg.order_cap_ticks == 0.0
    monkeypatch.setenv("RISK_ORDER_CAP_BPS", "5")       # 5 bps = 0.05% of price
    monkeypatch.setenv("RISK_ORDER_CAP_TICKS", "2")     # 2 ticks floor for thin names
    monkeypatch.setenv("RISK_LIMIT_ORDERS_ENABLED", "true")
    cfg2 = load_risk_config()
    assert cfg2.limit_orders_enabled is True
    assert cfg2.order_cap_bps == 5.0
    assert cfg2.order_cap_ticks == 2.0
```
- [ ] Run `pytest tests/test_config.py::test_loader_reads_limit_order_knobs_with_safe_defaults -q` — expect **FAIL** (`TypeError: __init__() got an unexpected keyword` / `AttributeError`, fields don't exist yet).
- [ ] Implement in `autotrader/config.py`. Add fields to `RiskConfig` (after `option_max_risk_pct: float = 0.02`), with a HUMAN-REVIEW comment matching the existing options block:
```python
    # --- Protective limit orders (additive; DEFAULT-OFF => current MARKET behavior).
    # HUMAN-REVIEW risk params. When limit_orders_enabled is False the engine emits
    # the exact MARKET orders it does today. When True, entries/rebalance/option legs
    # are submitted as capped marketable limits: cap = max(order_cap_bps/1e4 * price,
    # order_cap_ticks * tick). Trailing-stop exits are unaffected. ---
    limit_orders_enabled: bool = False
    order_cap_bps: float = 0.0
    order_cap_ticks: float = 0.0
```
- [ ] Add to the `RiskConfig(...)` construction in `load_risk_config()` (alongside the other reads):
```python
        limit_orders_enabled=_b("RISK_LIMIT_ORDERS_ENABLED", False),
        order_cap_bps=_f("RISK_ORDER_CAP_BPS", 0.0),
        order_cap_ticks=_f("RISK_ORDER_CAP_TICKS", 0.0),
```
- [ ] Run `pytest tests/test_config.py -q` — expect **PASS** (new test + all existing config tests).
- [ ] Commit:
```
git commit -am "feat(config): add order-cap bps/ticks + limit-orders flag (default off)

New HUMAN-REVIEW risk params RISK_ORDER_CAP_BPS/RISK_ORDER_CAP_TICKS/
RISK_LIMIT_ORDERS_ENABLED. All default off, preserving current MARKET behavior.

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 2: Slippage-aware `SimBroker` — simulated bid/ask + MARKET pays spread, LIMIT marketability (§3.A)

Built **first** (the measuring stick), before any limit wiring, per the spec's "measuring stick first" decision.

**Files:** `autotrader/sim_broker.py` (`__init__` 16-28; `place_order` fill logic 45-68; add `_bid`/`_ask`/`_fill_price` helpers); Test: `tests/test_sim_broker_slippage.py` (new). Existing `tests/test_sim_broker.py` must stay green.

**Interfaces:**
- Consumes: new `__init__` kwargs `spread_bps: float = 0.0` (half-spread each side, in bps of reference; `0.0` ⇒ zero spread = today's behavior), `slippage_bps: float = 0.0` (extra adverse move on MARKET fills beyond the touch).
- Produces: MARKET buy fills at `ask * (1 + slippage_bps/1e4)`, MARKET sell at `bid * (1 - slippage_bps/1e4)`; LIMIT fills at its `limit_price` only if marketable (BUY `limit_price >= ask`, SELL `limit_price <= bid`), else rests as `SUBMITTED` in `_open`.
- Backward compatibility: with default `spread_bps=0.0, slippage_bps=0.0`, bid==ask==ref and a marketable-or-not LIMIT at the ref fills at the ref — identical to today. A LIMIT strictly through the touch still fills at its own price today (there is no spread), matching current `price = req.limit_price` behavior.

- [ ] Add RED tests to `tests/test_sim_broker_slippage.py`:
```python
import pytest
from autotrader.domain import OrderRequest, OrderState
from autotrader.sim_broker import SimBroker


def _mkt(side, cid, qty=10):
    return OrderRequest(symbol="US.AAPL", side=side, qty=qty, order_type="MARKET",
                        limit_price=None, client_order_id=cid)


def _lim(side, price, cid, qty=10):
    return OrderRequest(symbol="US.AAPL", side=side, qty=qty, order_type="LIMIT",
                        limit_price=price, client_order_id=cid)


def test_market_buy_pays_the_ask_with_spread():
    # ref 100, half-spread 50 bps -> ask 100.5, bid 99.5.
    b = SimBroker(quotes={"US.AAPL": 100.0}, cash=100000.0, spread_bps=50.0)
    ack = b.place_order(_mkt("BUY", "b1"))
    assert ack.state is OrderState.FILLED
    fill = b.reconcile_fills(None)[0]
    assert fill.price == pytest.approx(100.5)   # buys at the far touch (ask), not mid


def test_market_sell_receives_the_bid_with_spread():
    b = SimBroker(quotes={"US.AAPL": 100.0}, cash=100000.0, spread_bps=50.0)
    b._positions.clear()
    ack = b.place_order(_mkt("SELL", "s1"))
    fill = b.reconcile_fills(None)[0]
    assert fill.price == pytest.approx(99.5)     # sells at the far touch (bid)


def test_market_slippage_moves_fill_adversely_beyond_touch():
    # 50 bps spread + 20 bps slippage: buy fills at ask*(1+0.002)=100.5*1.002.
    b = SimBroker(quotes={"US.AAPL": 100.0}, cash=100000.0,
                  spread_bps=50.0, slippage_bps=20.0)
    b.place_order(_mkt("BUY", "b1"))
    assert b.reconcile_fills(None)[0].price == pytest.approx(100.5 * 1.002)


def test_marketable_limit_buy_fills_at_its_own_price():
    # limit 100.6 >= ask 100.5 -> marketable, fills at the LIMIT price (100.6).
    b = SimBroker(quotes={"US.AAPL": 100.0}, cash=100000.0, spread_bps=50.0)
    ack = b.place_order(_lim("BUY", 100.6, "l1"))
    assert ack.state is OrderState.FILLED
    assert b.reconcile_fills(None)[0].price == pytest.approx(100.6)


def test_non_marketable_limit_buy_rests_unfilled():
    # limit 100.0 < ask 100.5 -> not marketable, rests, no position change.
    b = SimBroker(quotes={"US.AAPL": 100.0}, cash=100000.0, spread_bps=50.0)
    ack = b.place_order(_lim("BUY", 100.0, "l2"))
    assert ack.state is OrderState.SUBMITTED
    assert b.get_account().position_qty("US.AAPL") == 0
    assert len(b.get_open_orders()) == 1


def test_non_marketable_limit_sell_rests_unfilled():
    b = SimBroker(quotes={"US.AAPL": 100.0}, cash=100000.0, spread_bps=50.0)
    ack = b.place_order(_lim("SELL", 100.0, "l3"))   # 100.0 > bid 99.5 -> not marketable
    assert ack.state is OrderState.SUBMITTED
    assert len(b.get_open_orders()) == 1


def test_zero_spread_zero_slippage_is_todays_behavior():
    # Defaults: bid==ask==ref==100; MARKET fills at 100 exactly (regression).
    b = SimBroker(quotes={"US.AAPL": 100.0}, cash=100000.0)
    b.place_order(_mkt("BUY", "b1"))
    assert b.reconcile_fills(None)[0].price == pytest.approx(100.0)
```
- [ ] Run `pytest tests/test_sim_broker_slippage.py -q` — expect **FAIL** (`__init__` has no `spread_bps` kwarg; MARKET fills at mid today).
- [ ] Implement in `autotrader/sim_broker.py`. Extend `__init__`:
```python
    def __init__(self, quotes: Dict[str, float], cash: float = 10000.0,
                 auto_fill: bool = True, option_chains=None,
                 spread_bps: float = 0.0, slippage_bps: float = 0.0):
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
        self._chains = {(u.upper(), r.upper()): list(v)
                        for (u, r), v in (option_chains or {}).items()}
```
- [ ] Add the touch/fill helpers (near `get_quote`):
```python
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
```
- [ ] Rewrite the fill decision in `place_order` (replace lines 49-66). A TRAILING_STOP still rests unconditionally; a LIMIT rests unless marketable; a MARKET fills at the far touch (+slippage); a marketable LIMIT fills at its own price:
```python
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
```
  > Note: the idempotency guard (`if req.client_order_id in self._acks_by_cid`) at the top of `place_order` stays unchanged, above this block.
- [ ] Run `pytest tests/test_sim_broker_slippage.py tests/test_sim_broker.py -q` — expect **PASS** (new slippage tests + all existing sim-broker tests, including trailing-stop-rests and the LIMIT-at-100 order in `_req` which is marketable at zero spread).
- [ ] Run `pytest tests/test_options_planner.py tests/test_options_e2e.py tests/test_engine_rebalance_run.py -q` — expect **PASS** (option/rebalance suites use `SimBroker` with default zero spread; behavior unchanged).
- [ ] Commit:
```
git commit -am "feat(sim-broker): spread/slippage model — MARKET pays the touch, LIMIT marketability

Adds spread_bps/slippage_bps (default 0 => today's behavior). MARKET fills at the
far touch (buy@ask/sell@bid) plus optional slippage; LIMIT fills at its own price
only when marketable vs the simulated touch, else rests. This makes paper execution
quality measurable before wiring capped limits.

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 3: Capped marketable-limit helper `autotrader/limit_pricing.py` (§3.B math)

**Files:** `autotrader/limit_pricing.py` (new, pure — no SDK, importable with no OpenD); Test: `tests/test_limit_pricing.py` (new).

**Interfaces:**
- Consumes: `capped_limit_price(side: Side, ref_price: float, cfg: RiskConfig) -> float`. `Side` is `"BUY"|"SELL"`. Reads `cfg.order_cap_bps`, `cfg.order_cap_ticks`.
- Produces: BUY → `ref_price + cap`; SELL → `ref_price - cap`; where `cap = max(order_cap_bps/1e4 * ref_price, order_cap_ticks * tick_size(ref_price))`. The bps term governs normal names; the tick floor protects low-priced/thin names (CLOV $5.23, MARA $13.60). Result is rounded to the tick and never ≤ 0.
- Consumes/Produces: `tick_size(price: float) -> float` — US equity ladder: `< $1.00 → 0.0001`, `>= $1.00 → 0.01`. (Simple two-tier ladder is sufficient for the allow-listed names; documented as the tick model.)

- [ ] Add RED tests to `tests/test_limit_pricing.py`:
```python
import pytest
from autotrader.config import RiskConfig
from autotrader.limit_pricing import capped_limit_price, tick_size


def _cfg(bps=0.0, ticks=0.0):
    return RiskConfig(trading_env="PAPER", min_confidence=0.6, max_order_notional=2000,
                      max_position_qty=100, daily_loss_limit=500, max_gross_exposure=10000,
                      allowed_symbols=frozenset({"US.AAPL"}),
                      order_cap_bps=bps, order_cap_ticks=ticks)


def test_tick_size_two_tier_ladder():
    assert tick_size(0.50) == pytest.approx(0.0001)
    assert tick_size(5.23) == pytest.approx(0.01)
    assert tick_size(200.0) == pytest.approx(0.01)


def test_buy_cap_adds_bps_above_reference():
    # 5 bps of 200 = 0.10 -> BUY limit 200.10.
    cfg = _cfg(bps=5.0)
    assert capped_limit_price("BUY", 200.0, cfg) == pytest.approx(200.10)


def test_sell_cap_subtracts_bps_below_reference():
    cfg = _cfg(bps=5.0)
    assert capped_limit_price("SELL", 200.0, cfg) == pytest.approx(199.90)


def test_tick_floor_dominates_on_low_priced_name():
    # CLOV-like $5.23, 1 bps of 5.23 = 0.0005 (< a tick), tick floor of 2 ticks = 0.02.
    cfg = _cfg(bps=1.0, ticks=2.0)
    assert capped_limit_price("BUY", 5.23, cfg) == pytest.approx(5.25)   # 5.23 + 0.02
    assert capped_limit_price("SELL", 5.23, cfg) == pytest.approx(5.21)  # 5.23 - 0.02


def test_bps_dominates_on_high_priced_name():
    # 5 bps of 200 = 0.10 > 2 ticks (0.02); bps term wins.
    cfg = _cfg(bps=5.0, ticks=2.0)
    assert capped_limit_price("BUY", 200.0, cfg) == pytest.approx(200.10)


def test_result_is_rounded_to_the_tick():
    # 3 bps of 13.60 = 0.00408 -> caps to a whole tick multiple.
    cfg = _cfg(bps=3.0, ticks=1.0)
    px = capped_limit_price("BUY", 13.60, cfg)
    assert round(px / 0.01) == pytest.approx(px / 0.01)   # exact tick multiple
    assert px >= 13.60


def test_zero_caps_returns_reference_rounded():
    cfg = _cfg(bps=0.0, ticks=0.0)
    assert capped_limit_price("BUY", 100.0, cfg) == pytest.approx(100.0)
```
- [ ] Run `pytest tests/test_limit_pricing.py -q` — expect **FAIL** (`ModuleNotFoundError: autotrader.limit_pricing`).
- [ ] Implement `autotrader/limit_pricing.py`:
```python
"""Pure helper: compute a capped marketable-limit price from a reference price.
No SDK, no I/O — importable with no OpenD. cap = max(bps term, tick-floor term);
the bps term governs normal names, the tick floor protects low-priced/thin names.
The cap is applied around the single reference price the Broker.get_quote contract
returns (see plan Investigation findings: true live bid/ask is not plumbed through
get_quote in v1)."""
from __future__ import annotations

from autotrader.config import RiskConfig
from autotrader.domain import Side


def tick_size(price: float) -> float:
    """US equity minimum price increment (two-tier ladder): sub-$1 names quote in
    1/100c, $1-and-up names in 1c. Sufficient for the allow-listed universe."""
    return 0.0001 if price < 1.0 else 0.01


def capped_limit_price(side: Side, ref_price: float, cfg: RiskConfig) -> float:
    """Marketable limit price: BUY = ref + cap, SELL = ref - cap, where
    cap = max(order_cap_bps/1e4 * ref, order_cap_ticks * tick). Rounded to the
    tick; never <= 0."""
    tick = tick_size(ref_price)
    cap = max(cfg.order_cap_bps / 1e4 * ref_price, cfg.order_cap_ticks * tick)
    raw = ref_price + cap if side == "BUY" else ref_price - cap
    # Round to the nearest tick; a SELL can never round to <= 0.
    px = round(raw / tick) * tick
    return px if px > 0 else tick
```
- [ ] Run `pytest tests/test_limit_pricing.py -q` — expect **PASS**.
- [ ] Run `pytest tests/test_no_sdk_in_core.py -q` — expect **PASS** (new module imports only `config`/`domain`, no SDK).
- [ ] Commit:
```
git commit -am "feat(limit-pricing): capped marketable-limit helper with tick floor

capped_limit_price(side, ref, cfg) = ref +/- max(bps*ref, ticks*tick), rounded to
the US equity tick ladder. bps governs normal names; the tick floor protects
low-priced/thin names (CLOV/MARA). Pure module, no SDK.

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 4: Wire capped limits into strategy signals & rebalance; flag-off = byte-for-byte MARKET (§3.B + §3.D regression)

**Files:** `autotrader/main.py` (`_route_signal` order construction ~127-129; `submit_rebalance_order` order construction ~274-278); Test: `tests/test_limit_order_wiring.py` (new). `tests/test_main_loop.py` and `tests/test_engine_rebalance_submit.py` must stay green.

**Interfaces:**
- Consumes: `self._cfg.limit_orders_enabled`, `self._cfg.order_cap_bps`, `self._cfg.order_cap_ticks`, and the `price`/`ref_price` already in scope at each site; `capped_limit_price` from Task 3.
- Produces: a private helper on `TradeEngine`:
  `_order_kind(side: Side, ref_price: float) -> tuple[OrderType, Optional[float]]` returning `("MARKET", None)` when the flag is off (unchanged), or `("LIMIT", capped_limit_price(side, ref_price, cfg))` when on.
- Invariant: **trailing-stop and option-leg construction do not call `_order_kind`** (option legs handled in Task 5; trailing stops never change).

- [ ] Add RED tests to `tests/test_limit_order_wiring.py` (reuse the `_cfg`/`_engine` style from `test_main_loop.py`):
```python
import pytest
from autotrader.config import RiskConfig
from autotrader.sim_broker import SimBroker
from autotrader.strategies.threshold import ThresholdStrategy, StrategyParams
from autotrader.main import TradeEngine
from autotrader.rebalance import RebalanceTrade


def _cfg(**over):
    base = dict(trading_env="PAPER", min_confidence=0.6, max_order_notional=200000,
                max_position_qty=1000, daily_loss_limit=500, max_gross_exposure=1_000_000,
                allowed_symbols=frozenset({"US.AAPL"}))
    base.update(over)
    return RiskConfig(**base)


def _engine(broker, cfg, tmp_path, qty=10):
    strat = ThresholdStrategy(StrategyParams(symbol="US.AAPL", entry_price=100.0,
                                             stop_loss_pct=0.05, take_profit_pct=0.10,
                                             confidence=0.7))
    return TradeEngine(broker=broker, strategy=strat, cfg=cfg,
                       order_qty=qty, audit_path=str(tmp_path / "audit.jsonl"))


def _last_intent(audit_path):
    import json
    with open(audit_path) as f:
        intents = [json.loads(l) for l in f if json.loads(l)["action"] == "intent"]
    return intents[-1]


def test_flag_off_signal_is_market_byte_for_byte(tmp_path):
    b = SimBroker(quotes={"US.AAPL": 101.0}, cash=1_000_000.0)
    eng = _engine(b, _cfg(limit_orders_enabled=False), tmp_path)
    assert eng.tick().action == "ORDER_PLACED"
    intent = _last_intent(str(tmp_path / "audit.jsonl"))
    assert intent["order_type"] == "MARKET"
    assert intent["limit_price"] is None            # exactly today's request shape


def test_flag_on_signal_emits_capped_buy_limit(tmp_path):
    b = SimBroker(quotes={"US.AAPL": 101.0}, cash=1_000_000.0, spread_bps=10.0)
    eng = _engine(b, _cfg(limit_orders_enabled=True, order_cap_bps=20.0), tmp_path)
    assert eng.tick().action == "ORDER_PLACED"      # marketable: 20bps cap > 10bps spread
    intent = _last_intent(str(tmp_path / "audit.jsonl"))
    assert intent["order_type"] == "LIMIT"
    assert intent["limit_price"] == pytest.approx(101.0 + 101.0 * 0.0020)  # 101 + 20bps


def test_flag_off_rebalance_sell_is_market(tmp_path):
    b = SimBroker(quotes={"US.AAPL": 100.0}, cash=1_000_000.0)
    b._positions["US.AAPL"] = __import__("autotrader.domain", fromlist=["Position"]).Position("US.AAPL", 50, 100.0)
    eng = _engine(b, _cfg(limit_orders_enabled=False), tmp_path)
    trade = RebalanceTrade("US.AAPL", "SELL", 10, "TRIM", 40)
    assert eng.submit_rebalance_order(trade, 100.0, "rbal-x").action == "ORDER_PLACED"
    assert _last_intent(str(tmp_path / "audit.jsonl"))["order_type"] == "MARKET"


def test_flag_on_rebalance_sell_emits_capped_sell_limit(tmp_path):
    from autotrader.domain import Position
    b = SimBroker(quotes={"US.AAPL": 100.0}, cash=1_000_000.0, spread_bps=5.0)
    b._positions["US.AAPL"] = Position("US.AAPL", 50, 100.0)
    eng = _engine(b, _cfg(limit_orders_enabled=True, order_cap_bps=10.0), tmp_path)
    trade = RebalanceTrade("US.AAPL", "SELL", 10, "TRIM", 40)
    assert eng.submit_rebalance_order(trade, 100.0, "rbal-x").action == "ORDER_PLACED"
    intent = _last_intent(str(tmp_path / "audit.jsonl"))
    assert intent["order_type"] == "LIMIT"
    assert intent["limit_price"] == pytest.approx(100.0 - 100.0 * 0.0010)  # 100 - 10bps
```
- [ ] Run `pytest tests/test_limit_order_wiring.py -q` — expect **FAIL** (`_order_kind` doesn't exist; requests are still MARKET, `limit_price None`, so the flag-on assertions fail).
- [ ] Implement the helper on `TradeEngine` (in `autotrader/main.py`, near the other private helpers):
```python
    def _order_kind(self, side, ref_price: float):
        """(order_type, limit_price) for an equity entry/exit/rebalance leg.
        Flag OFF -> ('MARKET', None), byte-for-byte today's behavior. Flag ON ->
        a capped marketable LIMIT around ref_price. Trailing stops and option legs
        do NOT use this."""
        if not self._cfg.limit_orders_enabled:
            return "MARKET", None
        from autotrader.limit_pricing import capped_limit_price  # local: keep import cheap
        return "LIMIT", capped_limit_price(side, ref_price, self._cfg)
```
- [ ] Update the `_route_signal` order construction (replace the `req = OrderRequest(... order_type="MARKET", limit_price=None ...)` at ~128-129):
```python
        otype, lpx = self._order_kind(signal.direction, price)
        req = OrderRequest(symbol=signal.symbol, side=signal.direction, qty=eff_qty,
                           order_type=otype, limit_price=lpx, client_order_id=cid)
```
- [ ] Update `submit_rebalance_order` construction (replace the `req = OrderRequest(... order_type="MARKET", limit_price=None ...)` at ~276-278):
```python
        otype, lpx = self._order_kind(trade.side, ref_price)
        req = OrderRequest(symbol=trade.symbol, side=trade.side, qty=trade.qty,
                           order_type=otype, limit_price=lpx,
                           client_order_id=cid)
```
  > `_flatten_all` and `apply_risk_check`'s HALT path call `submit_rebalance_order`, so loss-halt liquidation SELLs also become capped limits when the flag is on — but Task 6's escalation guarantees they still complete via MARKET fallback. Trailing-stop attach/consolidate paths (`_attach_trailing_stop`, `consolidate_stop`) are **not** touched.
- [ ] Run `pytest tests/test_limit_order_wiring.py -q` — expect **PASS**.
- [ ] Run `pytest tests/test_main_loop.py tests/test_engine_rebalance_submit.py tests/test_engine_rebalance_run.py tests/test_engine_risk_check.py -q` — expect **PASS** (flag defaults off ⇒ every existing engine test still emits MARKET; behavior unchanged).
- [ ] Commit:
```
git commit -am "feat(engine): capped marketable limits for signals + rebalance (flag-gated)

_order_kind returns MARKET/None when RISK_LIMIT_ORDERS_ENABLED is off (byte-for-byte
current behavior) and a capped LIMIT around the reference price when on. Wired into
strategy-signal and rebalance submission. Trailing stops and option legs untouched.

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 5: Wire capped limits into option legs (§3.B — option legs)

**Files:** `autotrader/options/planner.py` (leg `OrderRequest` construction ~108-115; the leg premium `q.premium` is the reference); Test: `tests/test_limit_order_wiring.py` (extend) — add option-leg cases. `tests/test_options_planner.py` / `test_options_e2e.py` stay green.

**Interfaces:**
- Consumes: `cfg.limit_orders_enabled`, `cfg.order_cap_bps`, `cfg.order_cap_ticks`, the leg's `q.premium` (reference), `spec.side`; `capped_limit_price`.
- Produces: each leg's `OrderRequest.order_type`/`limit_price` — `("MARKET", None)` when flag off (unchanged), or `("LIMIT", capped_limit_price(spec.side, q.premium, cfg))` when on. The cap is applied around the option **premium** (the reference the risk core already uses for option legs via `leg.quote.premium`, `main.py:204,216`).
- Note: `risk_core._evaluate_option_leg` reads `req.limit_price` when `order_type == "LIMIT"` (`risk_core.py:137`); a capped limit near the premium keeps the premium-budget math consistent.

- [ ] Add RED tests to `tests/test_limit_order_wiring.py` (build a plan directly, assert leg request shape). Reuse the `_call_chain`/`_asof` helpers pattern from `test_options_planner.py`:
```python
def _put_chain(asof):
    from datetime import timedelta
    from autotrader.options.chain import OptionQuote
    return [OptionQuote("US.AAPL260721P190000", "US.AAPL", asof + timedelta(days=35),
                        190, "PUT", -0.30, 4.0)]


def _overlay_cfg(**over):
    base = dict(trading_env="PAPER", min_confidence=0.6, max_order_notional=200000,
                max_position_qty=1000, daily_loss_limit=500, max_gross_exposure=1_000_000,
                allowed_symbols=frozenset({"US.AAPL"}),
                allowed_overlays=frozenset({"PROTECTIVE_PUT"}), max_option_contracts=5,
                option_max_risk_pct=0.5)
    base.update(over)
    return RiskConfig(**base)


def _build_plan(cfg):
    from datetime import date
    from autotrader.domain import Signal, OverlayType, Position, AccountSnapshot
    from autotrader.options.planner import build_overlay_plan, OverlayPlan
    asof = date(2026, 6, 16)
    b = SimBroker(quotes={"US.AAPL": 200.0},
                  option_chains={("US.AAPL", "PUT"): _put_chain(asof)})
    snap = AccountSnapshot(cash=1_000_000.0, total_assets=1_000_000.0, day_pnl=0.0,
                           stale=False, positions=(Position("US.AAPL", 100, 200.0),))
    sig = Signal("US.AAPL", "BUY", 0.9, "hedge", overlay=OverlayType.PROTECTIVE_PUT)
    plan = build_overlay_plan(sig, snap, b, cfg, "sig-1", asof)
    assert isinstance(plan, OverlayPlan)
    return plan


def test_flag_off_option_leg_is_market():
    plan = _build_plan(_overlay_cfg(limit_orders_enabled=False))
    leg = plan.legs[0]
    assert leg.request.order_type == "MARKET"
    assert leg.request.limit_price is None


def test_flag_on_option_leg_is_capped_limit_around_premium():
    plan = _build_plan(_overlay_cfg(limit_orders_enabled=True, order_cap_bps=50.0))
    leg = plan.legs[0]
    assert leg.request.order_type == "LIMIT"
    # long PUT (BUY) premium 4.0 + 50bps of 4.0 = 4.02, rounded to the tick.
    assert leg.request.limit_price == pytest.approx(4.02)
```
- [ ] Run the two new tests: `pytest tests/test_limit_order_wiring.py -k option -q` — expect **FAIL** (leg is MARKET/None regardless of flag).
- [ ] Implement in `autotrader/options/planner.py`. Compute the leg order kind before constructing the request (inside the `for i, spec in enumerate(deff.legs)` loop, after `q` is selected and `qty` computed, ~line 108):
```python
        if cfg.limit_orders_enabled:
            from autotrader.limit_pricing import capped_limit_price  # local import
            otype, lpx = "LIMIT", capped_limit_price(spec.side, q.premium, cfg)
        else:
            otype, lpx = "MARKET", None
        cid = OrderRouter.make_client_order_id(
            q.code, spec.side, qty, f"{signal_id}-{overlay.value}-{i}")
        req = OrderRequest(symbol=q.code, side=spec.side, qty=qty,
                           order_type=otype, limit_price=lpx,
                           client_order_id=cid, option=contract,
                           position_effect=spec.position_effect,
                           correlation_id=corr)
```
- [ ] Run `pytest tests/test_limit_order_wiring.py -q` — expect **PASS** (option + equity cases).
- [ ] Run `pytest tests/test_options_planner.py tests/test_options_e2e.py tests/test_options_overlays.py tests/test_risk_core_options.py -q` — expect **PASS** (flag defaults off ⇒ legs stay MARKET).
- [ ] Commit:
```
git commit -am "feat(options): capped marketable limits for overlay legs (flag-gated)

Option legs use a LIMIT capped around the leg premium when RISK_LIMIT_ORDERS_ENABLED
is on; MARKET/None when off (unchanged). Keeps the risk-core premium-budget math
consistent (it reads limit_price for LIMIT legs).

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 6: Re-peg-then-market escalation (§3.C)

**Files:** `autotrader/main.py` (new `_submit_with_escalation` helper; call it from `_route_signal` and `submit_rebalance_order` in place of the bare `self._router.submit(req)` when the flag is on); Test: `tests/test_limit_escalation.py` (new).

**Interfaces:**
- Consumes: `req: OrderRequest` (a capped LIMIT), `ref_price: float`, `side`, plus `self._router`, `self._b.get_open_orders`, `self._b.cancel_order`, `capped_limit_price`, `evaluate` (risk core), `self._cfg`, `snap`.
- Produces: `OrderAck` — the terminal ack after at most: (1) submit capped limit; (2) if still working, cancel + resubmit **once** pegged to the current touch (re-fetched quote); (3) if still working, submit a MARKET so a risk exit completes. Each resubmission uses a distinct `client_order_id` (suffix `-peg`, `-mkt`) so the router does not dedupe them to the original.
- "Still working" is decided by `client_order_id ∈ {ack.client_order_id for ack in broker.get_open_orders()}` (spec Open Item 3 finding). The re-peg and market resubmissions each pass through `evaluate(...)` again (risk core stays the sole gate — a re-peg/market fallback never bypasses risk).
- Bounded window: a single re-peg attempt (no busy-wait loop; `SimBroker` fills or rests synchronously). On live OpenD the window is one `get_open_orders()` poll per stage — no `time.sleep` (CLAUDE.md).

- [ ] Add RED tests to `tests/test_limit_escalation.py`. Use `SimBroker` with a spread wide enough that the initial cap is **not** marketable, so the limit rests and forces escalation:
```python
import pytest
from autotrader.config import RiskConfig
from autotrader.domain import OrderRequest, OrderState, Position
from autotrader.sim_broker import SimBroker
from autotrader.strategies.threshold import ThresholdStrategy, StrategyParams
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
```
- [ ] Run `pytest tests/test_limit_escalation.py -q` — expect **FAIL** (`_route_signal` submits once via `self._router.submit` and returns; a rested non-marketable limit leaves position 0, so `position_qty == 10` fails).
- [ ] Implement `_submit_with_escalation` on `TradeEngine` and route equity submissions through it. Add the helper:
```python
    def _submit_with_escalation(self, req: OrderRequest, snap, ref_price: float):
        """Submit a capped LIMIT; if it rests unfilled, cancel + re-peg once to the
        current touch; if still unfilled, submit MARKET so a risk exit completes.
        Each stage re-runs the risk core (it stays the sole gate) and uses a distinct
        client_order_id so the router does not dedupe. Only used when the flag is on
        and the order is a LIMIT; MARKET/TRAILING_STOP requests submit once as before."""
        if req.order_type != "LIMIT":
            return self._router.submit(req)
        ack = self._router.submit(req)

        def _still_working(a) -> bool:
            working = {o.client_order_id for o in self._b.get_open_orders()}
            return a.client_order_id in working

        if not _still_working(ack):
            return ack   # marketable limit filled (or terminal) on the first pass

        # Stage 2: cancel the resting limit, re-peg to the CURRENT touch.
        if ack.broker_order_id:
            try:
                self._b.cancel_order(ack.broker_order_id)
            except BrokerError as e:
                logger.warning("escalation: cancel %s failed: %s", ack.broker_order_id, e)
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
            if not _still_working(ack):
                return ack

        # Stage 3: MARKET fallback so the order (esp. a risk exit) completes.
        if ack.broker_order_id:
            try:
                self._b.cancel_order(ack.broker_order_id)
            except BrokerError as e:
                logger.warning("escalation: cancel %s failed: %s", ack.broker_order_id, e)
        mkt_cid = req.client_order_id + "-mkt"
        mkt = OrderRequest(symbol=req.symbol, side=req.side, qty=req.qty,
                           order_type="MARKET", limit_price=None,
                           client_order_id=mkt_cid, option=req.option,
                           position_effect=req.position_effect,
                           correlation_id=req.correlation_id)
        decision = evaluate(mkt, snap, self._cfg, ref_price=cur)
        if not decision.approved:
            logger.warning("escalation: MARKET fallback rejected by risk: %s", decision.reason)
            return ack
        return self._router.submit(mkt)
```
  > `BrokerError` is already imported? Confirm — `main.py` imports from `autotrader.domain`; add `BrokerError` to that import line if absent. Note the equity path never sets `option`; carrying it keeps the helper reusable, and `OrderRequest.__post_init__` allows `option=None`.
- [ ] Route the equity submission points through the helper. In `_route_signal`, replace `ack = self._router.submit(req)` (~136) with `ack = self._submit_with_escalation(req, snap, price)`. In `submit_rebalance_order`, replace `ack = self._router.submit(req)` (~284) with `ack = self._submit_with_escalation(req, snap, ref_price)`.
  > When the flag is off, `req.order_type == "MARKET"`, so `_submit_with_escalation` submits once and returns — identical to today. Trailing-stop attach/consolidate and option-leg submission in `_route_overlay` still call `self._router.submit` directly (option-leg escalation is out of scope; overlay legs keep their long-before-short ordering).
- [ ] Run `pytest tests/test_limit_escalation.py -q` — expect **PASS**.
- [ ] Run `pytest tests/test_main_loop.py tests/test_engine_rebalance_submit.py tests/test_engine_rebalance_run.py tests/test_engine_risk_check.py tests/test_router.py -q` — expect **PASS** (flag-off is MARKET → single submit; router idempotency preserved).
- [ ] Commit:
```
git commit -am "feat(engine): re-peg-then-market escalation for capped limits

_submit_with_escalation submits a capped LIMIT, re-pegs once to the current touch if
it rests, then falls back to MARKET so risk exits always complete. Each stage re-runs
the risk core and uses a distinct client_order_id. MARKET orders submit once (flag-off
unchanged); trailing stops untouched.

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 7: Full-suite regression + flag-off byte-for-byte confirmation (§3.D)

**Files:** none changed. Test: full offline suite.

**Interfaces:** Consumes: entire `tests/` tree. Produces: green suite as the completion gate.

- [ ] Run the whole offline suite: `pytest -q` — expect **PASS** (all pre-existing tests plus the four new files; no skips beyond the pre-existing live-only ones).
- [ ] Confirm the "flag off = current behavior" guarantee explicitly by running the regression tests together: `pytest tests/test_main_loop.py tests/test_limit_order_wiring.py::test_flag_off_signal_is_market_byte_for_byte tests/test_limit_order_wiring.py::test_flag_off_rebalance_sell_is_market tests/test_sim_broker.py -q` — expect **PASS**.
- [ ] Run `pytest tests/test_no_sdk_in_core.py -q` — expect **PASS** (new `limit_pricing.py` and all edits import no SDK in core).
- [ ] No new commit required if the suite is green (all work committed in Tasks 1-6). If any incidental fixup was needed, commit it:
```
git commit -am "test(limit-orders): full-suite regression pass; flag-off parity confirmed

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Self-review against §3

- **§3.A (slippage-aware SimBroker):** Task 2 — simulated bid/ask via `spread_bps`, MARKET at far touch + `slippage_bps`, marketable-LIMIT fills at its price, non-marketable rests. Built before any wiring (measuring-stick-first). Covered.
- **§3.B (capped marketable-limit construction):** Task 3 (helper: `SELL = ref − cap`, `BUY = ref + cap`, `cap = max(bps, ticks)`, tick floor on low-priced names) + Tasks 4-5 (wired into strategy signals `main.py`, rebalance `main.py`, option legs `planner.py`; trailing stops unchanged; reuses `OrderRequest.limit_price` + broker `LIMIT→NORMAL`). Covered. **Deviation from spec's literal "bid − cap / ask + cap":** per the Investigation finding, `Broker.get_quote` returns one reference price (last today), so the cap is applied around `ref_price` and the simulated bid/ask lives only in `SimBroker`. This is called out in the header note and Task 0; plumbing true live bid/ask through `get_quote` is a documented follow-up.
- **§3.C (re-peg-then-market escalation):** Task 6 — submit capped limit; if still working, cancel + re-peg once to current touch; else MARKET; risk core re-evaluated at each stage; distinct cids; fill status via `get_open_orders()` membership (no `time.sleep`). Covered.
- **§3.D (config keys + default off):** Task 1 (three `RISK_*` keys, human-review comment) + explicit flag-off byte-for-byte regression tests in Tasks 4 and 7. Covered.
- **§3 Testing bullets:** MARKET pays spread / marketable LIMIT fills / non-marketable rests (Task 2); cap math incl. tick floor (Task 3); escalation unfilled→re-peg→market (Task 6); flag off = current behavior (Tasks 4, 7). All covered.
- **Placeholders / undefined symbols:** none. `capped_limit_price`, `tick_size`, `_order_kind`, `_submit_with_escalation`, `spread_bps`, `slippage_bps` all defined in-plan; `evaluate`, `OrderRouter`, `OrderRequest`, `BrokerError`, `Position`, `RebalanceTrade`, `capped_limit_price` grounded in read source (`main.py`, `router.py`, `domain.py`, `rebalance.py`).
- **Type consistency:** `Side = Literal["BUY","SELL"]` and `OrderType`/`Optional[float]` from `domain.py`; `RiskConfig` new fields typed `float`/`bool`; `_order_kind` returns `(OrderType, Optional[float])`; `capped_limit_price` returns `float`. Consistent.
