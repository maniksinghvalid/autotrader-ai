# N-day Breakout Entry — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the internal strategy's static `ENTRY_PRICE` with an N-day breakout entry (buy on a new N-day high), keeping the strategy pure and making "no data" fail safe to *no entry*.

**Architecture:** Four small units — a pure `BreakoutStrategy` (entry = `price > ref_high`, exits unchanged), a stateful `BreakoutReference` daily-caching the N-day high, a new `Broker.recent_high` data method (SimBroker + MoomooBroker), and `TradeEngine.tick()` wiring that fetches the reference and passes it in. `ENTRY_PRICE` is retired; `ENTRY_BREAKOUT_LOOKBACK` (default 20) and `STRATEGY_ENABLED` (default true) replace it.

**Tech Stack:** Python 3, pytest, moomoo SDK (live only, confined to `MoomooBroker`), existing `Broker`/`TradeEngine`/`SimBroker` abstractions.

**Spec:** `docs/superpowers/specs/2026-07-01-breakout-entry-design.md`

## Global Constraints

- **Paper-only.** No change to `TRADING_ENV`/risk-core behavior. Never call `unlock_trade`.
- **Fail-safe entry:** any missing/failed reference → **no entry**. There is no absolute-price fallback anywhere. A `None` reference must never produce a BUY.
- **Every moomoo SDK call checks `ret_code == RET_OK`** (use `self._ok(ret)`), logs non-OK with context, returns `None`. No bare `except: pass`.
- **Breakout is strict `>`** and uses the last N **completed** daily bars (today's forming bar excluded).
- **Config lives in env/`config/`, never hardcoded.** `ENTRY_BREAKOUT_LOOKBACK`, `STRATEGY_ENABLED` are signal-shaping (read in `main()`), kept out of `RiskConfig`.
- **TDD:** write the failing test first, watch it fail, minimal code, watch it pass, commit. One logical change per commit.
- Commit trailer on every commit: `Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>`.

## File Structure

- Create `autotrader/strategies/exits.py` — shared long-exit rule (stop/target).
- Create `autotrader/strategies/breakout.py` — `BreakoutParams` + `BreakoutStrategy` (pure).
- Create `autotrader/breakout_reference.py` — `BreakoutReference` (daily cache, stateful).
- Modify `autotrader/strategies/threshold.py` — use shared exits; add ignored `ref_high` param.
- Modify `autotrader/broker.py` — add `recent_high` to the interface.
- Modify `autotrader/sim_broker.py` — implement `recent_high` (+ `recent_highs` ctor arg).
- Modify `autotrader/moomoo_broker.py` — implement live `recent_high` (pragma no cover).
- Modify `autotrader/main.py` — engine `strategy_enabled` + `breakout_ref`; `tick()` gate + ref; `build_engine` forwarding; `main()` wiring; retire `ENTRY_PRICE`.
- Modify `RUNBOOK.md` — config docs.
- Tests: `tests/test_strategy_exits.py`, `tests/test_breakout_strategy.py`, `tests/test_breakout_reference.py`, `tests/test_moomoo_broker_offline.py` (add), `tests/test_sim_broker.py` (add), `tests/test_main_loop.py` (add).

---

### Task 1: Shared long-exit helper + ThresholdStrategy refactor

**Files:**
- Create: `autotrader/strategies/exits.py`
- Modify: `autotrader/strategies/threshold.py`
- Test: `tests/test_strategy_exits.py` (new), `tests/test_strategy_threshold.py` (must stay green)

**Interfaces:**
- Produces: `manage_long_exit(symbol: str, price: float, position: Optional[Position], stop_loss_pct: float, take_profit_pct: float, confidence: float) -> Optional[Signal]` — a SELL `Signal` when an open long hits stop/target, else `None` (incl. when flat).

- [ ] **Step 1: Write the failing test** — `tests/test_strategy_exits.py`

```python
from autotrader.domain import Position
from autotrader.strategies.exits import manage_long_exit


def _pos(qty, avg):
    return Position("US.AAPL", qty, avg)


def test_stop_loss_triggers_sell():
    s = manage_long_exit("US.AAPL", 95.0, _pos(10, 100.0), 0.05, 0.10, 0.7)
    assert s is not None and s.direction == "SELL" and "stop-loss" in s.rationale


def test_take_profit_triggers_sell():
    s = manage_long_exit("US.AAPL", 110.0, _pos(10, 100.0), 0.05, 0.10, 0.7)
    assert s is not None and s.direction == "SELL" and "take-profit" in s.rationale


def test_within_band_returns_none():
    assert manage_long_exit("US.AAPL", 102.0, _pos(10, 100.0), 0.05, 0.10, 0.7) is None


def test_flat_returns_none():
    assert manage_long_exit("US.AAPL", 102.0, None, 0.05, 0.10, 0.7) is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_strategy_exits.py -q`
Expected: FAIL — `ModuleNotFoundError: autotrader.strategies.exits`

- [ ] **Step 3: Create the helper** — `autotrader/strategies/exits.py`

```python
"""Shared long-exit rule for the internal strategies: stop-loss / take-profit off
avg cost. One place so BreakoutStrategy and ThresholdStrategy cannot drift."""
from __future__ import annotations

from typing import Optional

from autotrader.domain import Position, Signal


def manage_long_exit(symbol: str, price: float, position: Optional[Position],
                     stop_loss_pct: float, take_profit_pct: float,
                     confidence: float) -> Optional[Signal]:
    """SELL Signal when an open long hits its stop or target, else None (including
    when flat — the caller owns the entry rule)."""
    held = position.qty if position else 0
    if held <= 0 or position is None:
        return None
    change = (price - position.avg_price) / position.avg_price
    if change <= -stop_loss_pct:
        return Signal(symbol, "SELL", confidence, f"stop-loss hit ({change:.2%})")
    if change >= take_profit_pct:
        return Signal(symbol, "SELL", confidence, f"take-profit hit ({change:.2%})")
    return None
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/test_strategy_exits.py -q`
Expected: PASS (4 passed)

- [ ] **Step 5: Refactor ThresholdStrategy to use the helper + accept ignored `ref_high`**

Replace the `evaluate` method in `autotrader/strategies/threshold.py` (keep `StrategyParams` unchanged), and add the import:

```python
from autotrader.strategies.exits import manage_long_exit
```

```python
    def evaluate(self, price: float, position: Optional[Position],
                 ref_high: Optional[float] = None) -> Optional[Signal]:
        # ref_high is accepted for a uniform engine call path (see BreakoutStrategy)
        # and ignored here — this strategy enters on an absolute threshold.
        held = position.qty if position else 0
        if held > 0:
            return manage_long_exit(self.p.symbol, price, position,
                                    self.p.stop_loss_pct, self.p.take_profit_pct,
                                    self.p.confidence)
        if held == 0 and price >= self.p.entry_price:
            return Signal(self.p.symbol, "BUY", self.p.confidence,
                          f"price {price} >= entry {self.p.entry_price}")
        return None
```

- [ ] **Step 6: Run threshold + exits tests to verify unchanged behavior**

Run: `python3 -m pytest tests/test_strategy_threshold.py tests/test_strategy_exits.py -q`
Expected: PASS (all green — ThresholdStrategy rationale strings unchanged)

- [ ] **Step 7: Commit**

```bash
git add autotrader/strategies/exits.py autotrader/strategies/threshold.py tests/test_strategy_exits.py
git commit -m "refactor(strategy): shared long-exit helper; threshold accepts ref_high"
```

---

### Task 2: BreakoutStrategy (pure)

**Files:**
- Create: `autotrader/strategies/breakout.py`
- Test: `tests/test_breakout_strategy.py`

**Interfaces:**
- Consumes: `manage_long_exit` (Task 1).
- Produces: `BreakoutParams(symbol, stop_loss_pct, take_profit_pct, confidence)` (frozen dataclass, validates like `StrategyParams`); `BreakoutStrategy(params)` with `.p` and `evaluate(price: float, position: Optional[Position], ref_high: Optional[float] = None) -> Optional[Signal]`. BUY when `flat and ref_high is not None and price > ref_high`; exits via `manage_long_exit`; `ref_high is None` → no entry.

- [ ] **Step 1: Write the failing test** — `tests/test_breakout_strategy.py`

```python
from autotrader.domain import Position
from autotrader.strategies.breakout import BreakoutParams, BreakoutStrategy


def _s():
    return BreakoutStrategy(BreakoutParams("US.AAPL", 0.05, 0.10, 0.7))


def test_breakout_above_ref_high_buys():
    sig = _s().evaluate(price=131.0, position=None, ref_high=130.5)
    assert sig is not None and sig.direction == "BUY" and "breakout" in sig.rationale


def test_equal_to_ref_high_does_not_buy():
    assert _s().evaluate(price=130.5, position=None, ref_high=130.5) is None


def test_below_ref_high_does_not_buy():
    assert _s().evaluate(price=129.0, position=None, ref_high=130.5) is None


def test_none_ref_high_never_buys():
    assert _s().evaluate(price=999.0, position=None, ref_high=None) is None


def test_holding_stop_loss_sells():
    sig = _s().evaluate(price=95.0, position=Position("US.AAPL", 10, 100.0), ref_high=None)
    assert sig is not None and sig.direction == "SELL" and "stop-loss" in sig.rationale


def test_holding_take_profit_sells():
    sig = _s().evaluate(price=110.0, position=Position("US.AAPL", 10, 100.0), ref_high=1.0)
    assert sig is not None and sig.direction == "SELL" and "take-profit" in sig.rationale


def test_holding_within_band_holds():
    assert _s().evaluate(price=101.0, position=Position("US.AAPL", 10, 100.0), ref_high=1.0) is None


def test_params_reject_nonpositive_stop():
    import pytest
    with pytest.raises(ValueError):
        BreakoutParams("US.AAPL", 0.0, 0.10, 0.7)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_breakout_strategy.py -q`
Expected: FAIL — `ModuleNotFoundError: autotrader.strategies.breakout`

- [ ] **Step 3: Create the strategy** — `autotrader/strategies/breakout.py`

```python
"""Stateless N-day breakout strategy. Pure: (price, position, ref_high) -> Signal | None.
Enters long only on a new N-day high (price > ref_high); the reference high is computed
by BreakoutReference and passed in — the strategy never fetches data. Exits on stop /
target via the shared helper. Fail-safe: ref_high None (no data) => NO entry, never
buy-at-open (CLAUDE.md: explicit stop + target; no state between ticks)."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from autotrader.domain import Position, Signal
from autotrader.strategies.exits import manage_long_exit


@dataclass(frozen=True)
class BreakoutParams:
    symbol: str
    stop_loss_pct: float
    take_profit_pct: float
    confidence: float

    def __post_init__(self):
        if self.stop_loss_pct <= 0:
            raise ValueError("stop_loss_pct must be > 0 (explicit stop required)")
        if self.take_profit_pct <= 0:
            raise ValueError("take_profit_pct must be > 0 (explicit target required)")
        if not (0.0 <= self.confidence <= 1.0):
            raise ValueError("confidence must be in [0,1]")


class BreakoutStrategy:
    def __init__(self, params: BreakoutParams):
        self.p = params

    def evaluate(self, price: float, position: Optional[Position],
                 ref_high: Optional[float] = None) -> Optional[Signal]:
        held = position.qty if position else 0
        if held > 0:
            return manage_long_exit(self.p.symbol, price, position,
                                    self.p.stop_loss_pct, self.p.take_profit_pct,
                                    self.p.confidence)
        # Flat: enter ONLY on a confirmed breakout. No reference => no entry.
        if held == 0 and ref_high is not None and price > ref_high:
            return Signal(self.p.symbol, "BUY", self.p.confidence,
                          f"breakout: {price} > {self.p.symbol} high {ref_high}")
        return None
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/test_breakout_strategy.py -q`
Expected: PASS (8 passed)

- [ ] **Step 5: Commit**

```bash
git add autotrader/strategies/breakout.py tests/test_breakout_strategy.py
git commit -m "feat(strategy): pure N-day breakout strategy (fail-safe on no reference)"
```

---

### Task 3: `Broker.recent_high` interface + SimBroker

**Files:**
- Modify: `autotrader/broker.py`
- Modify: `autotrader/sim_broker.py`
- Test: `tests/test_sim_broker.py` (add)

**Interfaces:**
- Produces: `Broker.recent_high(symbol: str, lookback: int) -> Optional[float]` (base raises `NotImplementedError`). `SimBroker(..., recent_highs: Optional[Dict[str, float]] = None)`; `SimBroker.recent_high(symbol, lookback)` returns `recent_highs.get(symbol)` (None when unset).

- [ ] **Step 1: Write the failing test** — append to `tests/test_sim_broker.py`

```python
def test_recent_high_returns_configured_value():
    from autotrader.sim_broker import SimBroker
    b = SimBroker(quotes={"US.AAPL": 100.0}, recent_highs={"US.AAPL": 130.5})
    assert b.recent_high("US.AAPL", 20) == 130.5


def test_recent_high_none_when_unset():
    from autotrader.sim_broker import SimBroker
    b = SimBroker(quotes={"US.AAPL": 100.0})
    assert b.recent_high("US.AAPL", 20) is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_sim_broker.py -k recent_high -q`
Expected: FAIL — `TypeError: __init__() got an unexpected keyword argument 'recent_highs'`

- [ ] **Step 3: Add the interface method** — in `autotrader/broker.py`, after `get_touch` (around line 26):

```python
    def recent_high(self, symbol: str, lookback: int) -> Optional[float]:
        """Highest daily high over the last `lookback` COMPLETED trading days
        (today's forming bar excluded), or None on data failure / insufficient
        history. None fails safe upstream to 'no entry' — never a buy-at-open."""
        raise NotImplementedError
```

- [ ] **Step 4: Implement in SimBroker** — in `autotrader/sim_broker.py`

Add the ctor arg (extend the signature and store it):

```python
    def __init__(self, quotes: Dict[str, float], cash: float = 10000.0,
                 auto_fill: bool = True, option_chains=None,
                 spread_bps: float = 0.0, slippage_bps: float = 0.0,
                 recent_highs: Optional[Dict[str, float]] = None):
        ...
        self._recent_highs = dict(recent_highs or {})
```

Add the method (near `get_quote`):

```python
    def recent_high(self, symbol: str, lookback: int) -> Optional[float]:
        return self._recent_highs.get(symbol)
```

- [ ] **Step 5: Run test to verify it passes**

Run: `python3 -m pytest tests/test_sim_broker.py -k recent_high -q`
Expected: PASS (2 passed)

- [ ] **Step 6: Commit**

```bash
git add autotrader/broker.py autotrader/sim_broker.py tests/test_sim_broker.py
git commit -m "feat(broker): recent_high interface + SimBroker impl"
```

---

### Task 4: `BreakoutReference` (daily cache)

**Files:**
- Create: `autotrader/breakout_reference.py`
- Test: `tests/test_breakout_reference.py`

**Interfaces:**
- Consumes: `Broker.recent_high` (Task 3).
- Produces: `BreakoutReference(broker, lookback: int, today_fn=None)`; `high(symbol: str) -> Optional[float]` — computes once per trading day (keyed by `today_fn().isoformat()`), caches only non-None, recomputes on date-roll, warns at most once/day on None.

- [ ] **Step 1: Write the failing test** — `tests/test_breakout_reference.py`

```python
from datetime import date
from autotrader.breakout_reference import BreakoutReference


class _Broker:
    def __init__(self, values):
        self.values = list(values)   # popped per call
        self.calls = 0

    def recent_high(self, symbol, lookback):
        self.calls += 1
        return self.values.pop(0) if self.values else None


def test_fetches_once_per_day_then_caches():
    b = _Broker([130.5])
    ref = BreakoutReference(b, 20, today_fn=lambda: date(2026, 7, 2))
    assert ref.high("US.AAPL") == 130.5
    assert ref.high("US.AAPL") == 130.5   # cache hit, no second fetch
    assert b.calls == 1


def test_recomputes_on_date_roll():
    b = _Broker([130.5, 131.9])
    day = {"d": date(2026, 7, 2)}
    ref = BreakoutReference(b, 20, today_fn=lambda: day["d"])
    assert ref.high("US.AAPL") == 130.5
    day["d"] = date(2026, 7, 3)
    assert ref.high("US.AAPL") == 131.9
    assert b.calls == 2


def test_none_is_not_cached_and_retried():
    b = _Broker([None, 130.5])
    ref = BreakoutReference(b, 20, today_fn=lambda: date(2026, 7, 2))
    assert ref.high("US.AAPL") is None    # first fetch fails
    assert ref.high("US.AAPL") == 130.5   # retried same day, now succeeds
    assert b.calls == 2


def test_lookback_clamped_to_at_least_one():
    b = _Broker([130.5])
    ref = BreakoutReference(b, 0, today_fn=lambda: date(2026, 7, 2))
    ref.high("US.AAPL")
    assert ref._lookback == 1
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_breakout_reference.py -q`
Expected: FAIL — `ModuleNotFoundError: autotrader.breakout_reference`

- [ ] **Step 3: Create the reference** — `autotrader/breakout_reference.py`

```python
"""Daily-cached N-day high for the internal breakout strategy. The only stateful
piece: computes the reference once per trading day (keyed by today_fn), retries on
failure (a None result is NOT cached), and never lets a data problem become a
buy-at-open — a None simply means the strategy does not enter this tick."""
from __future__ import annotations

import logging
from datetime import date
from typing import Dict, Optional, Tuple

logger = logging.getLogger("autotrader.breakout")


class BreakoutReference:
    def __init__(self, broker, lookback: int, today_fn=None):
        self._b = broker
        self._lookback = max(1, int(lookback))
        self._today_fn = today_fn or date.today
        self._cache: Dict[Tuple[str, str], float] = {}   # (day_iso, symbol) -> high
        self._warned_day: Optional[str] = None

    def high(self, symbol: str) -> Optional[float]:
        day = self._today_fn().isoformat()
        key = (day, symbol)
        if key in self._cache:
            return self._cache[key]
        h = self._b.recent_high(symbol, self._lookback)
        if h is None:
            if self._warned_day != day:
                logger.warning("breakout: no %d-day high for %s today — strategy will "
                               "not enter until data is available (fail-safe)",
                               self._lookback, symbol)
                self._warned_day = day
            return None                       # NOT cached -> retried next tick
        self._cache[key] = float(h)
        return self._cache[key]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/test_breakout_reference.py -q`
Expected: PASS (4 passed)

- [ ] **Step 5: Commit**

```bash
git add autotrader/breakout_reference.py tests/test_breakout_reference.py
git commit -m "feat(strategy): BreakoutReference daily-cached N-day high (retries on failure)"
```

---

### Task 5: TradeEngine wiring — `strategy_enabled` + `breakout_ref`

**Files:**
- Modify: `autotrader/main.py` (TradeEngine `__init__` ~line 67-95; `tick()` ~line 144-161)
- Test: `tests/test_main_loop.py` (add)

**Interfaces:**
- Consumes: `BreakoutStrategy` (Task 2), `BreakoutReference` (Task 4).
- Produces: `TradeEngine(..., strategy_enabled: bool = True, breakout_ref=None)`. `tick()`: returns `TickResult("STRATEGY_DISABLED")` when `not strategy_enabled`; otherwise fetches `ref_high = breakout_ref.high(symbol)` (None if `breakout_ref is None`) and calls `self._strat.evaluate(price=price, position=pos, ref_high=ref_high)`.

- [ ] **Step 1: Write the failing test** — append to `tests/test_main_loop.py`

```python
def test_breakout_tick_places_on_new_high(tmp_path):
    from autotrader.strategies.breakout import BreakoutParams, BreakoutStrategy
    from autotrader.breakout_reference import BreakoutReference
    from datetime import date
    b = SimBroker(quotes={"US.AAPL": 131.0}, cash=100000.0,
                  recent_highs={"US.AAPL": 130.5})
    strat = BreakoutStrategy(BreakoutParams("US.AAPL", 0.05, 0.10, 0.7))
    ref = BreakoutReference(b, 20, today_fn=lambda: date(2026, 7, 2))
    eng = TradeEngine(broker=b, strategy=strat, cfg=_cfg(), order_qty=10,
                      audit_path=str(tmp_path / "audit.jsonl"), breakout_ref=ref)
    assert eng.tick().action == "ORDER_PLACED"
    assert b.get_account().position_qty("US.AAPL") == 10


def test_breakout_tick_no_signal_below_high(tmp_path):
    from autotrader.strategies.breakout import BreakoutParams, BreakoutStrategy
    from autotrader.breakout_reference import BreakoutReference
    from datetime import date
    b = SimBroker(quotes={"US.AAPL": 129.0}, cash=100000.0,
                  recent_highs={"US.AAPL": 130.5})
    strat = BreakoutStrategy(BreakoutParams("US.AAPL", 0.05, 0.10, 0.7))
    ref = BreakoutReference(b, 20, today_fn=lambda: date(2026, 7, 2))
    eng = TradeEngine(broker=b, strategy=strat, cfg=_cfg(), order_qty=10,
                      audit_path=str(tmp_path / "audit.jsonl"), breakout_ref=ref)
    assert eng.tick().action == "NO_SIGNAL"


def test_breakout_tick_no_entry_without_reference(tmp_path):
    from autotrader.strategies.breakout import BreakoutParams, BreakoutStrategy
    from autotrader.breakout_reference import BreakoutReference
    from datetime import date
    b = SimBroker(quotes={"US.AAPL": 999.0}, cash=100000.0)   # no recent_highs -> None
    strat = BreakoutStrategy(BreakoutParams("US.AAPL", 0.05, 0.10, 0.7))
    ref = BreakoutReference(b, 20, today_fn=lambda: date(2026, 7, 2))
    eng = TradeEngine(broker=b, strategy=strat, cfg=_cfg(), order_qty=10,
                      audit_path=str(tmp_path / "audit.jsonl"), breakout_ref=ref)
    assert eng.tick().action == "NO_SIGNAL"   # fail-safe: no ref, no buy


def test_disabled_strategy_tick_is_noop(tmp_path):
    strat = ThresholdStrategy(StrategyParams(symbol="US.AAPL", entry_price=100.0,
                                             stop_loss_pct=0.05, take_profit_pct=0.10,
                                             confidence=0.7))
    b = SimBroker(quotes={"US.AAPL": 101.0}, cash=100000.0)
    eng = TradeEngine(broker=b, strategy=strat, cfg=_cfg(), order_qty=10,
                      audit_path=str(tmp_path / "audit.jsonl"), strategy_enabled=False)
    assert eng.tick().action == "STRATEGY_DISABLED"
    assert b.get_account().position_qty("US.AAPL") == 0
```

> Note: the `_cfg` helper and `ThresholdStrategy`/`StrategyParams`/`TradeEngine` imports already exist at the top of `tests/test_main_loop.py` (US.AAPL allowed). Every Task-5 test RED-fails at collection with `TypeError: unexpected keyword argument` (`breakout_ref=`/`strategy_enabled=`) until Step 3 adds the ctor args.

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_main_loop.py -k "breakout or disabled_strategy" -q`
Expected: FAIL — `TypeError: __init__() got an unexpected keyword argument 'breakout_ref'`

- [ ] **Step 3: Add ctor args** — in `TradeEngine.__init__` (`autotrader/main.py`), extend the signature and store:

```python
                 session_id: "Optional[str]" = None, snapshot_cache_ticks: int = 1,
                 strategy_enabled: bool = True, breakout_ref=None):
```

Near the other assignments (e.g. after `self._strat = strategy`):

```python
        self._strategy_enabled = strategy_enabled
        self._breakout_ref = breakout_ref
```

- [ ] **Step 4: Gate + pass the reference in `tick()`**

In `tick()` (after the halt check, before `_account_for_tick`):

```python
        if not self._strategy_enabled:
            return TickResult("STRATEGY_DISABLED")
```

Change the strategy call from:

```python
        signal = self._strat.evaluate(price=price, position=pos)
```

to:

```python
        ref_high = self._breakout_ref.high(symbol) if self._breakout_ref is not None else None
        signal = self._strat.evaluate(price=price, position=pos, ref_high=ref_high)
```

- [ ] **Step 5: Run the new tests + the full suite**

Run: `python3 -m pytest tests/test_main_loop.py -q && python3 -m pytest -q`
Expected: PASS — new breakout/disabled tests green; **no regressions** (ThresholdStrategy still works because its `evaluate` now accepts `ref_high`, Task 1).

- [ ] **Step 6: Commit**

```bash
git add autotrader/main.py tests/test_main_loop.py
git commit -m "feat(engine): tick gates on strategy_enabled + passes breakout reference"
```

---

### Task 6: MoomooBroker.recent_high (live)

**Files:**
- Modify: `autotrader/moomoo_broker.py`
- Test: `tests/test_moomoo_broker_offline.py` (add)

**Interfaces:**
- Produces: `MoomooBroker.recent_high(symbol, lookback) -> Optional[float]` — fetches daily klines via the quote context, returns `max(high)` over the last `lookback` completed bars, `None` on non-OK / empty / insufficient / exception.

- [ ] **Step 1: Write the failing test** — append to `tests/test_moomoo_broker_offline.py`

Mirror the existing offline pattern in that file (a fake `_quote` with the needed method + a stub `self._c`). Example (adapt to the file's existing fake-context helper if one exists):

```python
def test_recent_high_returns_max_completed_high():
    from autotrader.moomoo_broker import MoomooBroker
    import pandas as pd
    b = MoomooBroker.__new__(MoomooBroker)          # bypass connect()
    b._c = _FakeCommon(ret_ok=True)                 # is_empty/safe_get/safe_float/RET_OK
    df = pd.DataFrame({"time_key": ["2026-06-30", "2026-07-01"],
                       "high": [128.0, 130.5]})
    b._quote = _FakeQuote(hist=("OK", df))
    assert b.recent_high("US.AAPL", 20) == 130.5


def test_recent_high_none_on_ret_error():
    from autotrader.moomoo_broker import MoomooBroker
    b = MoomooBroker.__new__(MoomooBroker)
    b._c = _FakeCommon(ret_ok=False)
    b._quote = _FakeQuote(hist=("ERR", "rate limited"))
    assert b.recent_high("US.AAPL", 20) is None


def test_recent_high_none_on_insufficient_bars():
    from autotrader.moomoo_broker import MoomooBroker
    import pandas as pd
    b = MoomooBroker.__new__(MoomooBroker)
    b._c = _FakeCommon(ret_ok=True)
    b._quote = _FakeQuote(hist=("OK", pd.DataFrame({"time_key": ["2026-07-01"], "high": [130.5]})))
    assert b.recent_high("US.AAPL", 20) is None
```

> Reuse or add small fakes `_FakeCommon` (with `RET_OK`, `is_empty`, `safe_get`, `safe_float`) and `_FakeQuote` (with `request_history_kline(code, start, end, ktype, ...) -> (ret, df, page_key)`) following the existing offline-test style in this file.

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_moomoo_broker_offline.py -k recent_high -q`
Expected: FAIL — `AttributeError: 'MoomooBroker' object has no attribute 'recent_high'`

- [ ] **Step 3: Implement** — in `autotrader/moomoo_broker.py`, in the `# --- market data ---` section (near `get_touch`):

```python
    def recent_high(self, symbol: str, lookback: int):  # pragma: no cover — live OpenD
        """Highest daily high over the last `lookback` COMPLETED trading days
        (today's forming bar excluded), or None on data failure / insufficient
        history. Quote-context call — no trade refresh tokens. Fail-safe: None."""
        from datetime import date as _date, timedelta
        lookback = max(1, int(lookback))
        today = _date.today()
        start = (today - timedelta(days=lookback * 2 + 10)).strftime("%Y-%m-%d")
        end = today.strftime("%Y-%m-%d")
        try:
            ret, data, _pk = self._quote.request_history_kline(
                symbol, start=start, end=end, ktype=self._c.KLType.K_DAY,
                autype=self._c.AuType.QFQ, max_count=lookback + 5)
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
            if h and h > 0:
                highs.append(h)
        completed = highs[-lookback:]
        if len(completed) < lookback:                # insufficient history -> no entry
            logger.info("recent_high %s: only %d/%d completed bars", symbol,
                        len(completed), lookback)
            return None
        return max(completed)
```

> If `self._c` does not already expose `KLType`/`AuType`, import them from `moomoo` inside this method (confined import, like `get_option_chain` imports `OptionType`): `from moomoo import KLType, AuType`.

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/test_moomoo_broker_offline.py -k recent_high -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add autotrader/moomoo_broker.py tests/test_moomoo_broker_offline.py
git commit -m "feat(broker): live MoomooBroker.recent_high via daily klines (fail-safe)"
```

---

### Task 7: Wire breakout into `build_engine` + `main()`; retire ENTRY_PRICE

**Files:**
- Modify: `autotrader/main.py` (`build_engine` ~836; `main()` ~876-905)
- Test: `tests/test_main_loop.py` (add a `build_engine` forwarding test)

**Interfaces:**
- Consumes: everything above.
- Produces: `build_engine(..., strategy_enabled: bool = True, breakout_ref=None)` forwards both to `TradeEngine`. `main()` reads `ENTRY_BREAKOUT_LOOKBACK` (default 20), `STRATEGY_ENABLED` (default true), builds `BreakoutStrategy` + `BreakoutReference`, and no longer reads `ENTRY_PRICE`.

- [ ] **Step 1: Write the failing test** — append to `tests/test_main_loop.py`

```python
def test_build_engine_forwards_strategy_flags(tmp_path):
    from autotrader.main import build_engine
    b = SimBroker(quotes={"US.AAPL": 101.0}, cash=100000.0)
    strat = ThresholdStrategy(StrategyParams(symbol="US.AAPL", entry_price=100.0,
                                             stop_loss_pct=0.05, take_profit_pct=0.10,
                                             confidence=0.7))
    eng = build_engine(b, strat, _cfg(), order_qty=1,
                       audit_path=str(tmp_path / "a.jsonl"),
                       strategy_enabled=False, breakout_ref="SENTINEL")
    assert eng._strategy_enabled is False
    assert eng._breakout_ref == "SENTINEL"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_main_loop.py -k build_engine_forwards -q`
Expected: FAIL — `TypeError: build_engine() got an unexpected keyword argument 'strategy_enabled'`

- [ ] **Step 3: Extend `build_engine`** — add the kwargs and forward:

```python
def build_engine(broker, strategy, cfg, *, order_qty: int, audit_path: str,
                 db=None, entry_gate=None, alert_url=None,
                 strategy_enabled: bool = True, breakout_ref=None) -> TradeEngine:
```

and in the returned `TradeEngine(...)` add:

```python
        strategy_enabled=strategy_enabled,
        breakout_ref=breakout_ref,
```

- [ ] **Step 4: Rewire `main()`** — replace the strategy-construction block (`autotrader/main.py` ~876-886) and the `build_engine(...)` call:

Replace the imports/strategy block:

```python
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
```

Delete the old `from autotrader.strategies.threshold import StrategyParams` line, the `entry_price = float(os.getenv("ENTRY_PRICE", "0"))` block, the `ENTRY_PRICE<=0` warning, and the old `ThresholdStrategy(StrategyParams(...))` construction.

After `broker.connect()` (broker exists), build the reference and pass both into `build_engine`:

```python
    breakout_ref = BreakoutReference(broker, lookback)
    engine = build_engine(broker, strat, cfg,
                          order_qty=int(os.getenv("ORDER_QTY", "1")),
                          audit_path=audit, db=db, entry_gate=gate,
                          alert_url=slack_url,
                          strategy_enabled=strategy_enabled, breakout_ref=breakout_ref)
```

> `main()` is `# pragma: no cover`; verify by import + a quick `python3 -c "import autotrader.main"` (no NameErrors) rather than a unit test.

- [ ] **Step 5: Run the forwarding test + import check + full suite**

Run: `python3 -m pytest tests/test_main_loop.py -k build_engine_forwards -q && python3 -c "import autotrader.main" && python3 -m pytest -q`
Expected: PASS; import clean; full suite green.

- [ ] **Step 6: Commit**

```bash
git add autotrader/main.py tests/test_main_loop.py
git commit -m "feat(engine): wire breakout strategy + reference; retire ENTRY_PRICE"
```

---

### Task 8: RUNBOOK documentation

**Files:**
- Modify: `RUNBOOK.md`

- [ ] **Step 1: Update the env-var table and troubleshooting**

- Remove the `ENTRY_PRICE` row and the "Bot buys immediately at startup | ENTRY_PRICE is 0…" troubleshooting row.
- Add rows:
  - `ENTRY_BREAKOUT_LOOKBACK | 20 | N completed daily bars for the breakout high. Higher = rarer, stronger breakouts.`
  - `STRATEGY_ENABLED | true | Internal strategy on/off. false = engine acts only on webhook + rebalance.`
- Add a one-line note under the strategy section: *"The internal strategy enters on a new N-day high (`price > N-day high`). If the daily klines can't be fetched, it does not enter — it never buys at open."*
- Keep the existing `STRATEGY_SYMBOL` / `RISK_ALLOWED_SYMBOLS` rows (still accurate).

- [ ] **Step 2: Commit**

```bash
git add RUNBOOK.md
git commit -m "docs(runbook): breakout entry config; drop ENTRY_PRICE"
```

---

## Self-Review

**Spec coverage:**
- BreakoutStrategy (pure, strict `>`, fail-safe on None) → Task 2. ✓
- BreakoutReference (daily cache, uncached-None retry, warn-once) → Task 4. ✓
- Broker.recent_high (SimBroker + MoomooBroker, exclude today, insufficient→None) → Tasks 3, 6. ✓
- tick() wiring (STRATEGY_ENABLED gate, pass ref) → Task 5. ✓
- Config (ENTRY_BREAKOUT_LOOKBACK, STRATEGY_ENABLED, retire ENTRY_PRICE) → Task 7. ✓
- Exit logic shared to avoid drift → Task 1 (spec said "shared helper"). ✓
- RUNBOOK → Task 8. ✓
- Fail-safe "no data → no entry, never buy-at-open" → asserted in Tasks 2, 4, 5, 6. ✓

**Placeholder scan:** no TBD/TODO; every code step shows full code. The MoomooBroker fakes (`_FakeCommon`/`_FakeQuote`) are described as "follow existing offline-test style" because the exact helper names depend on `tests/test_moomoo_broker_offline.py`'s current fixtures — the implementer adapts to what's there (the SDK call + fields are fully specified).

**Type consistency:** `evaluate(price, position, ref_high=None)` identical across `ThresholdStrategy` (Task 1) and `BreakoutStrategy` (Task 2); `recent_high(symbol, lookback)` identical across `Broker`/`SimBroker`/`MoomooBroker` (Tasks 3, 6) and consumed by `BreakoutReference` (Task 4); `strategy_enabled`/`breakout_ref` names identical across `TradeEngine` ctor, `tick()`, and `build_engine` (Tasks 5, 7).
