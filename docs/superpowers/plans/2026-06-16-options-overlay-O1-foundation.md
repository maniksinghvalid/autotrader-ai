# Options Overlay Trading — O1 Foundation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Place real single-leg option overlays (covered call, protective put) end-to-end through the existing webhook → inbox → trader pipeline, fully testable offline on SimBroker.

**Architecture:** Approach C (overlay expander). An overlay signal expands into 1–N leg `OrderRequest`s that flow through the **same** `OrderRouter` → `risk_core.evaluate` → broker path equities use. O1 adds: option domain types, an optional `overlay` field on the wire/domain signal, an `autotrader/options/` package (chain selection + overlay registry + planner), an option-aware risk-core branch, and a SimBroker synthetic option chain. Default-off (no `allowed_overlays`, `max_option_contracts=0`).

**Tech Stack:** Python 3, dataclasses, pydantic v2, pytest. No new third-party deps. Live option-chain fetch confined to `MoomooBroker` (SDK).

**Scope note:** This is O1 of a staged build (see `docs/superpowers/specs/2026-06-16-options-overlay-trading-design.md`). O2 = collar + multi-leg unwind; O3 = call diagonal + bear put spread; O4 = exit lifecycle. O1 implements only the two single-leg, share-covered overlays and the architecture the rest plug into. Exit rules are **declared** (data on the plan) in O1; exit *execution* is O4.

---

## File Structure

- **Create** `autotrader/options/__init__.py` — package marker.
- **Create** `autotrader/options/chain.py` — `OptionQuote`, pure `select_contract`, `to_contract`.
- **Create** `autotrader/options/overlays.py` — `LegSpec`, `ExitRule`, `OverlayDef`, `REGISTRY` (covered call, protective put).
- **Create** `autotrader/options/planner.py` — `OverlayLeg`, `OverlayPlan`, `OverlaySkip`, `build_overlay_plan`.
- **Modify** `autotrader/domain.py` — `OptionRight`, `PositionEffect`, `OverlayType`, `OptionContract`; extend `OrderRequest` (option/position_effect/correlation_id) and `Signal` (overlay).
- **Modify** `autotrader/signals/schema.py` — optional `overlay` Literal on `SignalChange`.
- **Modify** `autotrader/signals/normalize.py` — carry `overlay` onto `Signal`.
- **Modify** `autotrader/config.py` — option `RiskConfig` fields + env parsing.
- **Modify** `autotrader/risk_core.py` — option-aware leg branch.
- **Modify** `autotrader/broker.py` — `get_option_chain` base method.
- **Modify** `autotrader/sim_broker.py` — store/return synthetic chains.
- **Modify** `autotrader/moomoo_broker.py` — live `get_option_chain` (pragma: live-only).
- **Modify** `autotrader/main.py` — `_route_overlay` + overlay branch in `_route_signal`; `today_fn` on engine.
- **Modify** `docs/webhook-payload.schema.json` — add `overlay` property.
- **Create** `tests/test_options_chain.py`, `tests/test_options_overlays.py`, `tests/test_options_planner.py`, `tests/test_risk_core_options.py`, `tests/test_options_e2e.py`.

**Run the full suite with:** `python3 -m pytest -q` (from repo root).

---

## Task 1: Option domain types

**Files:**
- Modify: `autotrader/domain.py`
- Test: `tests/test_options_chain.py` (domain assertions live here to start)

- [ ] **Step 1: Write the failing test**

Create `tests/test_options_chain.py`:

```python
from datetime import date

import pytest

from autotrader.domain import (
    OptionContract, OrderRequest, OverlayType, Signal,
)


def test_option_contract_valid():
    c = OptionContract(underlying="US.AAPL", expiry=date(2026, 7, 17),
                       strike=200.0, right="CALL", code="US.AAPL260717C200000")
    assert c.multiplier == 100
    assert c.right == "CALL"


def test_option_contract_rejects_bad_strike():
    with pytest.raises(ValueError):
        OptionContract(underlying="US.AAPL", expiry=date(2026, 7, 17),
                       strike=0.0, right="CALL", code="x")


def test_option_contract_rejects_bad_right():
    with pytest.raises(ValueError):
        OptionContract(underlying="US.AAPL", expiry=date(2026, 7, 17),
                       strike=10.0, right="STRADDLE", code="x")


def test_order_request_carries_option_leg():
    c = OptionContract(underlying="US.AAPL", expiry=date(2026, 7, 17),
                       strike=200.0, right="CALL", code="US.AAPL260717C200000")
    req = OrderRequest(symbol=c.code, side="SELL", qty=1, order_type="MARKET",
                       limit_price=None, client_order_id="at-x", option=c,
                       position_effect="OPEN", correlation_id="corr-1")
    assert req.option is c
    assert req.position_effect == "OPEN"
    assert req.correlation_id == "corr-1"


def test_order_request_equity_defaults_unchanged():
    req = OrderRequest(symbol="US.AAPL", side="BUY", qty=10, order_type="MARKET",
                       limit_price=None, client_order_id="at-y")
    assert req.option is None
    assert req.position_effect == "OPEN"
    assert req.correlation_id is None


def test_signal_overlay_default_none():
    s = Signal(symbol="US.AAPL", direction="BUY", confidence=0.7, rationale="x")
    assert s.overlay is None
    assert Signal(symbol="US.AAPL", direction="SELL", confidence=0.7,
                  rationale="x", overlay=OverlayType.COVERED_CALL).overlay \
        is OverlayType.COVERED_CALL
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_options_chain.py -q`
Expected: FAIL — `ImportError: cannot import name 'OptionContract'`.

- [ ] **Step 3: Add domain types**

In `autotrader/domain.py`, add `date` to the imports at the top:

```python
from datetime import date
```

Replace the type aliases block (currently `Side`/`OrderType` near line 11) with:

```python
Side = Literal["BUY", "SELL"]
OrderType = Literal["MARKET", "LIMIT", "TRAILING_STOP"]
OptionRight = Literal["CALL", "PUT"]
PositionEffect = Literal["OPEN", "CLOSE"]


class OverlayType(enum.Enum):
    """Full option-strategy range. O1 implements COVERED_CALL + PROTECTIVE_PUT;
    the rest are declared so the schema/enum never needs to change to add them —
    the planner rejects any value with no registry entry (SKIP_UNSUPPORTED_OVERLAY)."""
    COVERED_CALL = "COVERED_CALL"
    PROTECTIVE_PUT = "PROTECTIVE_PUT"
    COLLAR = "COLLAR"
    CALL_DIAGONAL = "CALL_DIAGONAL"
    BEAR_PUT_SPREAD = "BEAR_PUT_SPREAD"


@dataclass(frozen=True)
class OptionContract:
    """A concrete tradable option. `code` is the moomoo option code (e.g.
    US.AAPL260717C200000). `multiplier` is shares per contract (US equity opts = 100)."""
    underlying: str
    expiry: date
    strike: float
    right: OptionRight
    code: str
    multiplier: int = 100

    def __post_init__(self):
        if self.right not in ("CALL", "PUT"):
            raise ValueError(f"OptionContract.right must be CALL/PUT, got {self.right!r}")
        if not math.isfinite(self.strike) or self.strike <= 0:
            raise ValueError(f"strike must be positive finite, got {self.strike}")
        if self.multiplier <= 0:
            raise ValueError(f"multiplier must be > 0, got {self.multiplier}")
```

Add `overlay` to `Signal` (after `stop_price`, line ~49):

```python
    overlay: Optional["OverlayType"] = None   # None = plain equity signal (default)
```

Extend `OrderRequest` — add three fields after `trail_percent` (line ~69):

```python
    option: Optional["OptionContract"] = None    # None = equity order (default)
    position_effect: PositionEffect = "OPEN"     # OPEN/CLOSE (close logic: O4)
    correlation_id: Optional[str] = None         # groups legs of one overlay (multi-leg: O2)
```

(No new validation needed in `OrderRequest.__post_init__`: `qty > 0` and the
LIMIT-needs-price rule already cover option legs; for an option leg `qty` is
contracts and `limit_price` is premium.)

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/test_options_chain.py -q`
Expected: PASS (6 passed).

- [ ] **Step 5: Commit**

```bash
git add autotrader/domain.py tests/test_options_chain.py
git commit -m "feat(domain): option contract + overlay types (additive)"
```

---

## Task 2: Wire `overlay` through schema → normalize → Signal

**Files:**
- Modify: `autotrader/signals/schema.py`
- Modify: `autotrader/signals/normalize.py`
- Test: `tests/test_options_chain.py` (append)

- [ ] **Step 1: Write the failing test**

Append to `tests/test_options_chain.py`:

```python
from autotrader.signals.schema import RoutineSignalPayload
from autotrader.signals.normalize import normalize_payload


def test_overlay_flows_schema_to_signal():
    raw = ('{"routine_id":"r1","timestamp":"2026-06-15T13:00:00Z",'
           '"signal_changes":[{"ticker":"CLOV","direction":"UP","points_delta":7,'
           '"driver":"Covered Call","overlay":"COVERED_CALL"}]}')
    payload = RoutineSignalPayload.model_validate_json(raw)
    assert payload.signal_changes[0].overlay == "COVERED_CALL"
    sigs = normalize_payload(payload)
    assert sigs[0].overlay is OverlayType.COVERED_CALL
    assert sigs[0].symbol == "US.CLOV"


def test_no_overlay_is_equity():
    raw = ('{"routine_id":"r1","timestamp":"2026-06-15T13:00:00Z",'
           '"signal_changes":[{"ticker":"AAPL","direction":"UP","points_delta":7}]}')
    sigs = normalize_payload(RoutineSignalPayload.model_validate_json(raw))
    assert sigs[0].overlay is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_options_chain.py -q`
Expected: FAIL — `AssertionError` (overlay is `None`; field not yet on schema/Signal path).

- [ ] **Step 3: Add the field + carry-through**

In `autotrader/signals/schema.py`, change the imports line:

```python
from typing import Dict, List, Literal, Optional
```

Add `overlay` to `SignalChange` (after `driver`):

```python
    # Optional explicit option-overlay intent (D4). Absent => plain equity
    # (today's behavior). Literal mirrors domain.OverlayType so this module stays
    # pydantic-only; normalize.py converts the string to the enum.
    overlay: Optional[Literal[
        "COVERED_CALL", "PROTECTIVE_PUT", "COLLAR",
        "CALL_DIAGONAL", "BEAR_PUT_SPREAD",
    ]] = None
```

In `autotrader/signals/normalize.py`, add the enum import:

```python
from autotrader.domain import OverlayType, Signal
```

In `normalize_change`, set `overlay` on the returned `Signal`:

```python
def normalize_change(change: SignalChange, confidence_scale: float = 10.0,
                     stop_price: Optional[float] = None) -> Signal:
    return Signal(
        symbol=_normalize_symbol(change.ticker),
        direction=_DIRECTION[change.direction],
        confidence=_confidence(change.points_delta, confidence_scale),
        rationale=f"{change.driver or 'external'}: "
                  f"{'/'.join(change.transition) or change.direction}",
        stop_price=stop_price,
        overlay=OverlayType(change.overlay) if change.overlay else None,
    )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/test_options_chain.py -q`
Expected: PASS (8 passed).

- [ ] **Step 5: Commit**

```bash
git add autotrader/signals/schema.py autotrader/signals/normalize.py tests/test_options_chain.py
git commit -m "feat(signals): carry optional overlay field schema->normalize->Signal"
```

---

## Task 3: Chain selection (`options/chain.py`)

**Files:**
- Create: `autotrader/options/__init__.py`
- Create: `autotrader/options/chain.py`
- Test: `tests/test_options_chain.py` (append)

- [ ] **Step 1: Write the failing test**

Append to `tests/test_options_chain.py`:

```python
from autotrader.options.chain import OptionQuote, select_contract, to_contract


def _chain(asof):
    from datetime import timedelta
    return [
        OptionQuote(code="C1", underlying="US.AAPL", expiry=asof + timedelta(days=10),
                    strike=205, right="CALL", delta=0.30, premium=2.0),   # too near (DTE 10)
        OptionQuote(code="C2", underlying="US.AAPL", expiry=asof + timedelta(days=35),
                    strike=210, right="CALL", delta=0.28, premium=1.5),   # in window, closest delta
        OptionQuote(code="C3", underlying="US.AAPL", expiry=asof + timedelta(days=35),
                    strike=220, right="CALL", delta=0.12, premium=0.6),   # in window, far delta
        OptionQuote(code="P1", underlying="US.AAPL", expiry=asof + timedelta(days=35),
                    strike=190, right="PUT", delta=-0.29, premium=1.4),
    ]


def test_select_picks_closest_delta_in_window():
    asof = date(2026, 6, 16)
    q = select_contract(_chain(asof), right="CALL", target_delta=0.30,
                        dte_min=30, dte_max=45, asof=asof)
    assert q.code == "C2"


def test_select_uses_abs_delta_for_puts():
    asof = date(2026, 6, 16)
    q = select_contract(_chain(asof), right="PUT", target_delta=0.30,
                        dte_min=30, dte_max=45, asof=asof)
    assert q.code == "P1"


def test_select_none_when_window_empty():
    asof = date(2026, 6, 16)
    assert select_contract(_chain(asof), right="CALL", target_delta=0.30,
                           dte_min=60, dte_max=90, asof=asof) is None


def test_to_contract_maps_fields():
    asof = date(2026, 6, 16)
    q = select_contract(_chain(asof), right="CALL", target_delta=0.30,
                        dte_min=30, dte_max=45, asof=asof)
    c = to_contract(q)
    assert c.code == "C2" and c.right == "CALL" and c.multiplier == 100
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_options_chain.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'autotrader.options'`.

- [ ] **Step 3: Create the package + selection logic**

Create empty `autotrader/options/__init__.py`:

```python
"""Options overlay package: chain selection, overlay registry, planner. No SDK
imports — live chain I/O is confined to autotrader.moomoo_broker."""
```

Create `autotrader/options/chain.py`:

```python
"""Pure option-contract selection. Given a chain snapshot (list of OptionQuote),
pick the contract closest to a target delta within a DTE window (D5). No I/O:
the broker supplies the snapshot; this module just chooses. Imports domain only."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import List, Optional

from autotrader.domain import OptionContract, OptionRight


@dataclass(frozen=True)
class OptionQuote:
    """One row of an option chain with the greeks/price the selector needs."""
    code: str
    underlying: str
    expiry: date
    strike: float
    right: OptionRight
    delta: float
    premium: float   # mid premium per share (x multiplier = contract cost)


def select_contract(quotes: List[OptionQuote], right: OptionRight,
                    target_delta: float, dte_min: int, dte_max: int,
                    asof: date) -> Optional[OptionQuote]:
    """Closest-to-target-|delta| contract of `right` whose DTE is in
    [dte_min, dte_max] and premium > 0. None if nothing qualifies."""
    target = abs(target_delta)
    candidates = [
        q for q in quotes
        if q.right == right and q.premium > 0
        and dte_min <= (q.expiry - asof).days <= dte_max
    ]
    if not candidates:
        return None
    return min(candidates, key=lambda q: abs(abs(q.delta) - target))


def to_contract(q: OptionQuote) -> OptionContract:
    return OptionContract(underlying=q.underlying, expiry=q.expiry,
                          strike=q.strike, right=q.right, code=q.code,
                          multiplier=100)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/test_options_chain.py -q`
Expected: PASS (12 passed).

- [ ] **Step 5: Commit**

```bash
git add autotrader/options/__init__.py autotrader/options/chain.py tests/test_options_chain.py
git commit -m "feat(options): pure delta+DTE contract selection"
```

---

## Task 4: Overlay registry (`options/overlays.py`)

**Files:**
- Create: `autotrader/options/overlays.py`
- Test: `tests/test_options_overlays.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_options_overlays.py`:

```python
from autotrader.domain import OverlayType
from autotrader.options.overlays import REGISTRY, OverlayDef, LegSpec, ExitRule


def test_covered_call_is_single_short_call_requiring_shares():
    d = REGISTRY[OverlayType.COVERED_CALL]
    assert d.requires_underlying is True
    assert len(d.legs) == 1
    leg = d.legs[0]
    assert leg.right == "CALL" and leg.side == "SELL" and leg.position_effect == "OPEN"


def test_protective_put_is_single_long_put_requiring_shares():
    d = REGISTRY[OverlayType.PROTECTIVE_PUT]
    assert d.requires_underlying is True
    leg = d.legs[0]
    assert leg.right == "PUT" and leg.side == "BUY"


def test_every_registered_overlay_declares_legs():
    # CLAUDE.md hard rule: a strategy without an exit is rejected. O1 declares the
    # exit at plan time from config; here we assert structural completeness.
    for d in REGISTRY.values():
        assert isinstance(d, OverlayDef)
        assert len(d.legs) >= 1


def test_unsupported_overlays_absent_from_registry():
    assert OverlayType.COLLAR not in REGISTRY
    assert OverlayType.CALL_DIAGONAL not in REGISTRY
    assert OverlayType.BEAR_PUT_SPREAD not in REGISTRY
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_options_overlays.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'autotrader.options.overlays'`.

- [ ] **Step 3: Create the registry**

Create `autotrader/options/overlays.py`:

```python
"""Overlay registry: each overlay is a stateless structural definition (its legs
+ whether it needs underlying shares). Concrete strike/expiry come from the chain
selector and config at plan time; the exit rule is attached from config by the
planner. O1 registers the two single-leg, share-covered overlays. Adding COLLAR /
spreads later is a new entry here — no schema or enum change. Imports domain only."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Tuple

from autotrader.domain import OptionRight, OverlayType, PositionEffect, Side


@dataclass(frozen=True)
class LegSpec:
    """One leg of an overlay, pre-contract-resolution. `ratio` is contracts per
    100 shares of underlying (1 = one contract per round lot)."""
    right: OptionRight
    side: Side
    position_effect: PositionEffect = "OPEN"
    ratio: int = 1


@dataclass(frozen=True)
class ExitRule:
    """Declared exit discipline (CLAUDE.md). Values sourced from config at plan
    time; ENFORCEMENT (the close scan) is O4 — O1 only records the declaration."""
    dte_to_close: int
    profit_target_pct: float
    close_on_reversal: bool = True


@dataclass(frozen=True)
class OverlayDef:
    requires_underlying: bool
    legs: Tuple[LegSpec, ...]


REGISTRY: Dict[OverlayType, OverlayDef] = {
    OverlayType.COVERED_CALL: OverlayDef(
        requires_underlying=True,
        legs=(LegSpec(right="CALL", side="SELL"),),
    ),
    OverlayType.PROTECTIVE_PUT: OverlayDef(
        requires_underlying=True,
        legs=(LegSpec(right="PUT", side="BUY"),),
    ),
}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/test_options_overlays.py -q`
Expected: PASS (4 passed).

- [ ] **Step 5: Commit**

```bash
git add autotrader/options/overlays.py tests/test_options_overlays.py
git commit -m "feat(options): overlay registry (covered call, protective put)"
```

---

## Task 5: Option config fields

**Files:**
- Modify: `autotrader/config.py`
- Test: `tests/test_risk_core_options.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_risk_core_options.py`:

```python
import os

from autotrader.config import load_risk_config


def test_option_config_defaults_off(monkeypatch):
    monkeypatch.setenv("RISK_ALLOWED_SYMBOLS", "US.AAPL")
    cfg = load_risk_config()
    assert cfg.allowed_overlays == frozenset()
    assert cfg.max_option_contracts == 0
    assert cfg.max_option_premium_per_trade == 0.0
    assert cfg.option_target_delta == 0.30
    assert cfg.option_dte_min == 30 and cfg.option_dte_max == 45


def test_option_config_parses_env(monkeypatch):
    monkeypatch.setenv("RISK_ALLOWED_SYMBOLS", "US.AAPL")
    monkeypatch.setenv("RISK_ALLOWED_OVERLAYS", "covered_call, protective_put")
    monkeypatch.setenv("RISK_MAX_OPTION_CONTRACTS", "5")
    monkeypatch.setenv("RISK_MAX_OPTION_PREMIUM_PER_TRADE", "800")
    cfg = load_risk_config()
    assert cfg.allowed_overlays == frozenset({"COVERED_CALL", "PROTECTIVE_PUT"})
    assert cfg.max_option_contracts == 5
    assert cfg.max_option_premium_per_trade == 800.0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_risk_core_options.py -q`
Expected: FAIL — `AttributeError: 'RiskConfig' object has no attribute 'allowed_overlays'`.

- [ ] **Step 3: Add config fields + parsing**

In `autotrader/config.py`, add fields to `RiskConfig` after `daily_loss_halt` (line ~37):

```python
    # --- Options overlays (additive; DEFAULT-OFF). allowed_overlays empty AND
    # max_option_contracts=0 both block option orders. Strike/expiry are chosen by
    # delta+DTE targets. All values are HUMAN-REVIEW risk limits. ---
    allowed_overlays: FrozenSet[str] = frozenset()
    max_option_contracts: int = 0
    max_option_premium_per_trade: float = 0.0
    option_target_delta: float = 0.30
    option_dte_min: int = 30
    option_dte_max: int = 45
    option_dte_to_close: int = 7        # exit declaration (enforced O4)
    option_profit_target_pct: float = 0.5
```

In `load_risk_config`, before the `return RiskConfig(`, parse overlays:

```python
    raw_overlays = os.getenv("RISK_ALLOWED_OVERLAYS", "")
    overlays = frozenset(
        o.strip().upper() for o in raw_overlays.split(",") if o.strip()
    )
```

Add these to the `RiskConfig(...)` constructor call (after `daily_loss_halt=daily_loss_halt,`):

```python
        allowed_overlays=overlays,
        max_option_contracts=int(_f("RISK_MAX_OPTION_CONTRACTS", 0)),
        max_option_premium_per_trade=_f("RISK_MAX_OPTION_PREMIUM_PER_TRADE", 0.0),
        option_target_delta=_f("RISK_OPTION_TARGET_DELTA", 0.30),
        option_dte_min=int(_f("RISK_OPTION_DTE_MIN", 30)),
        option_dte_max=int(_f("RISK_OPTION_DTE_MAX", 45)),
        option_dte_to_close=int(_f("RISK_OPTION_DTE_TO_CLOSE", 7)),
        option_profit_target_pct=_f("RISK_OPTION_PROFIT_TARGET_PCT", 0.5),
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/test_risk_core_options.py -q`
Expected: PASS (2 passed).

- [ ] **Step 5: Commit**

```bash
git add autotrader/config.py tests/test_risk_core_options.py
git commit -m "feat(config): default-off option overlay risk limits + delta/DTE targets"
```

---

## Task 6: Option-aware risk core

**Files:**
- Modify: `autotrader/risk_core.py`
- Test: `tests/test_risk_core_options.py` (append)

- [ ] **Step 1: Write the failing test**

Append to `tests/test_risk_core_options.py`:

```python
from datetime import date

from autotrader.domain import (
    AccountSnapshot, OptionContract, OrderRequest, Position,
)
from autotrader.risk_core import evaluate
from autotrader.config import RiskConfig


def _cfg(**over):
    base = dict(trading_env="PAPER", min_confidence=0.6, max_order_notional=2000,
                max_position_qty=100, daily_loss_limit=500, max_gross_exposure=50000,
                allowed_symbols=frozenset({"US.AAPL"}), daily_loss_halt=1000,
                allowed_overlays=frozenset({"COVERED_CALL", "PROTECTIVE_PUT"}),
                max_option_contracts=5, max_option_premium_per_trade=800.0)
    base.update(over)
    return RiskConfig(**base)


def _snap(aapl_shares=100):
    pos = (Position("US.AAPL", aapl_shares, 200.0),) if aapl_shares else ()
    return AccountSnapshot(cash=50000, total_assets=70000, day_pnl=0.0,
                           stale=False, positions=pos)


def _call(qty=1):
    c = OptionContract(underlying="US.AAPL", expiry=date(2026, 7, 17), strike=210,
                       right="CALL", code="US.AAPL260717C210000")
    return OrderRequest(symbol=c.code, side="SELL", qty=qty, order_type="MARKET",
                        limit_price=None, client_order_id="at-c", option=c,
                        position_effect="OPEN", correlation_id="k")


def _put(qty=1):
    c = OptionContract(underlying="US.AAPL", expiry=date(2026, 7, 17), strike=190,
                       right="PUT", code="US.AAPL260717P190000")
    return OrderRequest(symbol=c.code, side="BUY", qty=qty, order_type="MARKET",
                        limit_price=None, client_order_id="at-p", option=c,
                        position_effect="OPEN", correlation_id="k")


def test_covered_short_call_approved():
    d = evaluate(_call(qty=1), _snap(aapl_shares=100), _cfg(), ref_price=1.5)
    assert d.approved, d.reason


def test_uncovered_short_call_rejected():
    d = evaluate(_call(qty=1), _snap(aapl_shares=0), _cfg(), ref_price=1.5)
    assert not d.approved and "uncovered" in d.reason.lower()


def test_short_call_more_contracts_than_covered_rejected():
    # 1 contract covers 100 shares; 2 contracts need 200 shares.
    d = evaluate(_call(qty=2), _snap(aapl_shares=100), _cfg(), ref_price=1.5)
    assert not d.approved and "uncovered" in d.reason.lower()


def test_contracts_over_cap_rejected():
    d = evaluate(_call(qty=6), _snap(aapl_shares=600), _cfg(), ref_price=1.5)
    assert not d.approved and "contracts" in d.reason.lower()


def test_long_put_premium_cap_rejected():
    # 1 contract * premium 9.0 * 100 = 900 > 800 cap
    d = evaluate(_put(qty=1), _snap(), _cfg(), ref_price=9.0)
    assert not d.approved and "premium" in d.reason.lower()


def test_long_put_within_cap_approved():
    d = evaluate(_put(qty=1), _snap(), _cfg(), ref_price=1.4)
    assert d.approved, d.reason


def test_option_underlying_not_in_allowlist_rejected():
    d = evaluate(_put(qty=1), _snap(), _cfg(allowed_symbols=frozenset({"US.MSFT"})),
                 ref_price=1.4)
    assert not d.approved and "allow-list" in d.reason.lower()


def test_option_blocked_when_contracts_cap_zero():
    d = evaluate(_call(qty=1), _snap(), _cfg(max_option_contracts=0), ref_price=1.5)
    assert not d.approved
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_risk_core_options.py -q`
Expected: FAIL — option legs currently hit the equity long-only path (`SELL would short`) or `AttributeError`.

- [ ] **Step 3: Add the option branch**

In `autotrader/risk_core.py`, insert the option branch in `evaluate` immediately
after the stale-snapshot check (after the block ending line ~44, before the
daily-loss halt at step 3):

```python
    # Option legs follow their own rules (coverage, contracts, premium); the
    # equity long-only / notional / exposure caps below do not apply leg-by-leg.
    if req.option is not None:
        return _evaluate_option_leg(req, snapshot, cfg, ref_price)
```

Add the helper at the end of the module:

```python
def _evaluate_option_leg(req: OrderRequest, snapshot: AccountSnapshot,
                         cfg: RiskConfig, ref_price: Optional[float]) -> RiskDecision:
    """O1 option gate. Env + stale already checked by the caller.
    - underlying must be allow-listed
    - contracts <= max_option_contracts (cap 0 => options off)
    - a short OPEN leg must be share-covered (covered call) — never naked
    - a long (debit) OPEN leg's premium outlay is capped per trade
    Gross-exposure aggregation and the daily premium cap are later phases."""
    opt = req.option
    underlying = opt.underlying.upper()
    if underlying not in cfg.allowed_symbols:
        return RiskDecision(False, f"underlying {opt.underlying} not in allow-list")

    premium = req.limit_price if (req.order_type == "LIMIT" and req.limit_price) else ref_price
    if premium is None or not math.isfinite(premium) or premium <= 0:
        return RiskDecision(False, "no usable option premium for risk sizing")

    if req.qty > cfg.max_option_contracts:
        return RiskDecision(False,
                            f"contracts {req.qty} > cap {cfg.max_option_contracts}")

    if req.side == "SELL" and req.position_effect == "OPEN":
        need = req.qty * opt.multiplier
        held = snapshot.position_qty(underlying)
        if held < need:
            return RiskDecision(False,
                                f"uncovered short: held {held} < required {need} shares")
    else:
        cost = req.qty * premium * opt.multiplier
        if not math.isfinite(cost) or cost > cfg.max_option_premium_per_trade:
            return RiskDecision(False,
                                f"option premium {cost:.2f} > cap "
                                f"{cfg.max_option_premium_per_trade}")

    return RiskDecision(True, "OK")
```

(Note: `RiskConfig` is already imported in `risk_core.py`; no import change.)

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/test_risk_core_options.py -q`
Expected: PASS (11 passed).

- [ ] **Step 5: Run the full suite to confirm no regression**

Run: `python3 -m pytest -q`
Expected: PASS (all prior equity tests still green).

- [ ] **Step 6: Commit**

```bash
git add autotrader/risk_core.py tests/test_risk_core_options.py
git commit -m "feat(risk): option-aware leg gate (covered short, contract+premium caps)"
```

---

## Task 7: Broker chain interface + SimBroker synthetic chain

**Files:**
- Modify: `autotrader/broker.py`
- Modify: `autotrader/sim_broker.py`
- Test: `tests/test_options_planner.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_options_planner.py`:

```python
from datetime import date, timedelta

from autotrader.options.chain import OptionQuote
from autotrader.sim_broker import SimBroker


def _asof():
    return date(2026, 6, 16)


def _call_chain():
    a = _asof()
    return [
        OptionQuote("US.AAPL260721C210000", "US.AAPL", a + timedelta(days=35),
                    210, "CALL", 0.30, 1.5),
        OptionQuote("US.AAPL260721C220000", "US.AAPL", a + timedelta(days=35),
                    220, "CALL", 0.12, 0.6),
    ]


def test_sim_broker_returns_seeded_chain():
    b = SimBroker(quotes={"US.AAPL": 200.0},
                  option_chains={("US.AAPL", "CALL"): _call_chain()})
    rows = b.get_option_chain("US.AAPL", "CALL")
    assert [r.code for r in rows] == [q.code for q in _call_chain()]


def test_sim_broker_unknown_chain_is_empty():
    b = SimBroker(quotes={"US.AAPL": 200.0})
    assert b.get_option_chain("US.AAPL", "PUT") == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_options_planner.py -q`
Expected: FAIL — `AttributeError: 'SimBroker' object has no attribute 'get_option_chain'`.

- [ ] **Step 3: Add the interface + SimBroker support**

In `autotrader/broker.py`, add the import and a base method (after `get_quote`, line ~20):

```python
from autotrader.options.chain import OptionQuote
```

```python
    def get_option_chain(self, underlying: str, right: str) -> List["OptionQuote"]:
        """Return chain rows (strike/expiry/delta/premium) for one right.
        Live impl is MoomooBroker; base raises so a broker without it fails loud."""
        raise NotImplementedError
```

In `autotrader/sim_broker.py`, accept and store chains. Change the imports to add the type, then the constructor:

```python
from autotrader.options.chain import OptionQuote
```

Update `__init__` signature and body:

```python
    def __init__(self, quotes: Dict[str, float], cash: float = 10000.0,
                 auto_fill: bool = True, option_chains=None):
        self._quotes = dict(quotes)
        self._cash = cash
        self._positions: Dict[str, Position] = {}
        self._open: Dict[str, OrderAck] = {}
        self._fills: List[Fill] = []
        self._acks_by_cid: Dict[str, OrderAck] = {}
        self._auto_fill = auto_fill
        self._seq = 0
        # keyed by (underlying.upper(), right) -> List[OptionQuote]
        self._chains = {(u.upper(), r): list(v)
                        for (u, r), v in (option_chains or {}).items()}
```

Add the method (after `get_quote`):

```python
    def get_option_chain(self, underlying: str, right: str) -> List[OptionQuote]:
        return list(self._chains.get((underlying.upper(), right), []))
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/test_options_planner.py -q`
Expected: PASS (2 passed).

- [ ] **Step 5: Commit**

```bash
git add autotrader/broker.py autotrader/sim_broker.py tests/test_options_planner.py
git commit -m "feat(broker): get_option_chain interface + SimBroker synthetic chains"
```

---

## Task 8: Overlay planner (`options/planner.py`)

**Files:**
- Create: `autotrader/options/planner.py`
- Test: `tests/test_options_planner.py` (append)

- [ ] **Step 1: Write the failing test**

Append to `tests/test_options_planner.py`:

```python
from autotrader.config import RiskConfig
from autotrader.domain import AccountSnapshot, OverlayType, Position, Signal
from autotrader.options.planner import (
    build_overlay_plan, OverlayPlan, OverlaySkip,
)


def _put_chain():
    a = _asof()
    return [OptionQuote("US.AAPL260721P190000", "US.AAPL", a + timedelta(days=35),
                        190, "PUT", -0.29, 1.4)]


def _cfg(**over):
    base = dict(trading_env="PAPER", min_confidence=0.6, max_order_notional=2000,
                max_position_qty=100, daily_loss_limit=500, max_gross_exposure=50000,
                allowed_symbols=frozenset({"US.AAPL"}), daily_loss_halt=1000,
                allowed_overlays=frozenset({"COVERED_CALL", "PROTECTIVE_PUT"}),
                max_option_contracts=5, max_option_premium_per_trade=800.0)
    base.update(over)
    return RiskConfig(**base)


def _snap(shares=100):
    pos = (Position("US.AAPL", shares, 200.0),) if shares else ()
    return AccountSnapshot(cash=50000, total_assets=70000, day_pnl=0.0,
                           stale=False, positions=pos)


def _sig(overlay):
    return Signal(symbol="US.AAPL", direction="SELL", confidence=0.7,
                  rationale="x", overlay=overlay)


def _broker():
    return SimBroker(quotes={"US.AAPL": 200.0}, option_chains={
        ("US.AAPL", "CALL"): _call_chain(),
        ("US.AAPL", "PUT"): _put_chain(),
    })


def test_covered_call_plan_builds_short_call_leg():
    plan = build_overlay_plan(_sig(OverlayType.COVERED_CALL), _snap(200),
                              _broker(), _cfg(), "sig-1", _asof())
    assert isinstance(plan, OverlayPlan)
    assert len(plan.legs) == 1
    leg = plan.legs[0].request
    assert leg.side == "SELL" and leg.option.right == "CALL"
    assert leg.qty == 2                       # 200 shares -> 2 contracts
    assert leg.option.code == "US.AAPL260721C210000"   # closest 0.30 delta
    assert plan.exit.dte_to_close == 7


def test_protective_put_plan_builds_long_put_leg():
    plan = build_overlay_plan(_sig(OverlayType.PROTECTIVE_PUT), _snap(100),
                              _broker(), _cfg(), "sig-2", _asof())
    assert isinstance(plan, OverlayPlan)
    assert plan.legs[0].request.side == "BUY"
    assert plan.legs[0].request.option.right == "PUT"
    assert plan.legs[0].request.qty == 1


def test_skip_no_underlying():
    skip = build_overlay_plan(_sig(OverlayType.COVERED_CALL), _snap(0),
                              _broker(), _cfg(), "s", _asof())
    assert isinstance(skip, OverlaySkip) and skip.reason == "SKIP_NO_UNDERLYING"


def test_skip_overlay_disabled():
    skip = build_overlay_plan(_sig(OverlayType.COVERED_CALL), _snap(100),
                              _broker(), _cfg(allowed_overlays=frozenset()),
                              "s", _asof())
    assert isinstance(skip, OverlaySkip) and skip.reason == "SKIP_OVERLAY_DISABLED"


def test_skip_unsupported_overlay():
    skip = build_overlay_plan(_sig(OverlayType.COLLAR), _snap(100),
                              _broker(), _cfg(allowed_overlays=frozenset({"COLLAR"})),
                              "s", _asof())
    assert isinstance(skip, OverlaySkip) and skip.reason == "SKIP_UNSUPPORTED_OVERLAY"


def test_skip_no_contract_in_window():
    skip = build_overlay_plan(_sig(OverlayType.COVERED_CALL), _snap(100),
                              _broker(), _cfg(option_dte_min=60, option_dte_max=90),
                              "s", _asof())
    assert isinstance(skip, OverlaySkip) and skip.reason == "SKIP_NO_CONTRACT"


def test_contracts_capped_by_config():
    plan = build_overlay_plan(_sig(OverlayType.COVERED_CALL), _snap(1000),
                              _broker(), _cfg(max_option_contracts=3), "s", _asof())
    assert isinstance(plan, OverlayPlan) and plan.legs[0].request.qty == 3
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_options_planner.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'autotrader.options.planner'`.

- [ ] **Step 3: Create the planner**

Create `autotrader/options/planner.py`:

```python
"""Expand one overlay Signal into concrete leg OrderRequests, or a structured
skip. Pure given a chain provider (broker.get_option_chain): no order placement,
no SDK. Contract = delta+DTE selection (D5); short overlays require >=100 held
shares (D3). Imports domain, config, chain, overlays, router (cid helper)."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Tuple, Union

from autotrader.config import RiskConfig
from autotrader.domain import AccountSnapshot, OrderRequest, OverlayType, Signal
from autotrader.options.chain import OptionQuote, select_contract, to_contract
from autotrader.options.overlays import ExitRule, REGISTRY
from autotrader.router import OrderRouter


@dataclass(frozen=True)
class OverlayLeg:
    request: OrderRequest
    quote: OptionQuote


@dataclass(frozen=True)
class OverlayPlan:
    overlay: OverlayType
    underlying: str
    legs: Tuple[OverlayLeg, ...]
    exit: ExitRule
    correlation_id: str


@dataclass(frozen=True)
class OverlaySkip:
    overlay: OverlayType
    underlying: str
    reason: str   # SKIP_OVERLAY_DISABLED | SKIP_UNSUPPORTED_OVERLAY
                  # | SKIP_NO_UNDERLYING | SKIP_NO_CONTRACT


def build_overlay_plan(signal: Signal, snapshot: AccountSnapshot, chain_provider,
                       cfg: RiskConfig, signal_id: str,
                       asof: date) -> Union[OverlayPlan, OverlaySkip]:
    overlay = signal.overlay
    underlying = signal.symbol.upper()

    if overlay.value not in cfg.allowed_overlays:
        return OverlaySkip(overlay, underlying, "SKIP_OVERLAY_DISABLED")
    if overlay not in REGISTRY:
        return OverlaySkip(overlay, underlying, "SKIP_UNSUPPORTED_OVERLAY")

    deff = REGISTRY[overlay]
    held = snapshot.position_qty(underlying)
    contracts = min(held // 100, cfg.max_option_contracts)
    if deff.requires_underlying and contracts < 1:
        return OverlaySkip(overlay, underlying, "SKIP_NO_UNDERLYING")

    corr = f"ov-{signal_id}-{overlay.value}"
    legs = []
    for i, spec in enumerate(deff.legs):
        quotes = chain_provider.get_option_chain(underlying, spec.right)
        q = select_contract(quotes, spec.right, cfg.option_target_delta,
                            cfg.option_dte_min, cfg.option_dte_max, asof)
        if q is None:
            return OverlaySkip(overlay, underlying, "SKIP_NO_CONTRACT")
        contract = to_contract(q)
        qty = contracts * spec.ratio
        cid = OrderRouter.make_client_order_id(
            q.code, spec.side, qty, f"{signal_id}-{overlay.value}-{i}")
        req = OrderRequest(symbol=q.code, side=spec.side, qty=qty,
                           order_type="MARKET", limit_price=None,
                           client_order_id=cid, option=contract,
                           position_effect=spec.position_effect,
                           correlation_id=corr)
        legs.append(OverlayLeg(req, q))

    exit_rule = ExitRule(dte_to_close=cfg.option_dte_to_close,
                         profit_target_pct=cfg.option_profit_target_pct)
    return OverlayPlan(overlay, underlying, tuple(legs), exit_rule, corr)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/test_options_planner.py -q`
Expected: PASS (9 passed).

- [ ] **Step 5: Commit**

```bash
git add autotrader/options/planner.py tests/test_options_planner.py
git commit -m "feat(options): overlay planner (delta+DTE legs, structured skips)"
```

---

## Task 9: Engine wiring (`_route_overlay`)

**Files:**
- Modify: `autotrader/main.py`
- Test: `tests/test_options_e2e.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_options_e2e.py`:

```python
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
                  option_chains=_chains())
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
```

Note: the engine only needs *a* strategy instance; the overlay tests drive
`submit_external_signal`, not `tick()`.

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_options_e2e.py -q`
Expected: FAIL — `TypeError: __init__() got an unexpected keyword argument 'today_fn'`.

- [ ] **Step 3: Add `today_fn` + the overlay branch + `_route_overlay`**

In `autotrader/main.py`, add imports at top:

```python
from datetime import date
```

Extend `TradeEngine.__init__` signature and body (line ~36) — add `today_fn`:

```python
    def __init__(self, broker: Broker, strategy: ThresholdStrategy, cfg: RiskConfig,
                 order_qty: int, audit_path: str, db: "Optional[DB]" = None,
                 entry_gate: "Optional[EntryGate]" = None, today_fn=None):
        self._b = broker
        self._strat = strategy
        self._cfg = cfg
        self._qty = order_qty
        self._router = OrderRouter(broker, audit_path=audit_path)
        self._signal_seq = 0
        self._db = db
        self._gate = entry_gate
        self._today_fn = today_fn or date.today
```

In `_route_signal`, add the overlay branch right after the entry-window gate
block (after the `ENTRY_CLOSED` return, line ~81), before the `if self._db:`
performance-recording block:

```python
        # Option overlays expand into leg orders through this SAME audited path.
        if signal.overlay is not None:
            return self._route_overlay(signal, snap)
```

Add the method after `_route_signal` (before `_attach_trailing_stop`):

```python
    def _route_overlay(self, signal: Signal, snap) -> TickResult:
        """Expand an overlay signal into legs and place each through the audited
        router. Legs are ordered long-before-short so a covered structure's hedge
        is never momentarily naked. O1 overlays are single-leg; multi-leg
        atomic-unwind on partial failure is O2."""
        from autotrader.options.planner import build_overlay_plan, OverlayPlan

        self._signal_seq += 1
        signal_id = f"sig-{self._signal_seq}"
        plan = build_overlay_plan(signal, snap, self._b, self._cfg,
                                  signal_id, self._today_fn())
        if not isinstance(plan, OverlayPlan):
            logger.info("overlay skipped %s %s: %s",
                        plan.overlay.value, plan.underlying, plan.reason)
            return TickResult(plan.reason, f"{plan.overlay.value}:{plan.underlying}")

        if self._db:
            self._db.record_signal(
                symbol=signal.symbol, direction=signal.direction,
                confidence=signal.confidence,
                rationale=f"{plan.overlay.value}: {signal.rationale}",
                signal_id=signal_id)

        last_boid = None
        for leg in sorted(plan.legs, key=lambda l: 0 if l.request.side == "BUY" else 1):
            req = leg.request
            decision = evaluate(req, snap, self._cfg, ref_price=leg.quote.premium)
            if not decision.approved:
                logger.warning("overlay leg rejected (%s): %s", req.symbol, decision.reason)
                return TickResult("REJECTED_BY_RISK", decision.reason)
            ack = self._router.submit(req)
            if self._db:
                self._db.record_trade(
                    client_order_id=ack.client_order_id, symbol=req.symbol,
                    side=req.side, qty=req.qty, order_type=req.order_type,
                    limit_price=leg.quote.premium, broker_order_id=ack.broker_order_id,
                    state=ack.state.value)
            if ack.state is OrderState.UNKNOWN:
                return TickResult("ORDER_UNKNOWN", ack.client_order_id)
            if ack.state is OrderState.REJECTED:
                return TickResult("ORDER_REJECTED", ack.client_order_id)
            last_boid = ack.broker_order_id
        return TickResult("OVERLAY_PLACED", f"{plan.correlation_id}:{last_boid}")
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/test_options_e2e.py -q`
Expected: PASS (5 passed).

- [ ] **Step 5: Run the full suite**

Run: `python3 -m pytest -q`
Expected: PASS (all green, no regressions).

- [ ] **Step 6: Commit**

```bash
git add autotrader/main.py tests/test_options_e2e.py
git commit -m "feat(engine): route option overlays into legs via the audited path"
```

---

## Task 10: Live MoomooBroker chain (live-only) + webhook schema doc

**Files:**
- Modify: `autotrader/moomoo_broker.py`
- Modify: `docs/webhook-payload.schema.json`

- [ ] **Step 1: Add the live chain method (no offline test — live path)**

In `autotrader/moomoo_broker.py`, add `get_option_chain`. It is marked
`# pragma: no cover` (consistent with `heartbeat`/trailing-stop live paths) and
exercised only in the live paper smoke test below. Insert after `get_quote`
(line ~88):

```python
    def get_option_chain(self, underlying: str, right: str):  # pragma: no cover — live OpenD
        """Live option chain for `underlying` (e.g. US.AAPL) and `right`
        (CALL/PUT), returned as options.chain.OptionQuote rows. Confines all SDK
        option-isms here. Uses the vendored quote context: chain static info +
        a market snapshot for premium/greeks. Validated in the O1 live paper
        smoke test, not offline (no SDK/OpenD in CI)."""
        from datetime import datetime
        from autotrader.options.chain import OptionQuote
        from moomoo import OptionType, IndexOptionType  # noqa: F401 — confined import

        opt_type = OptionType.CALL if str(right).upper() == "CALL" else OptionType.PUT
        ret, chain = self._quote.get_option_chain(
            code=underlying, index_option_type=IndexOptionType.NORMAL,
            option_type=opt_type)
        if not self._ok(ret) or self._c.is_empty(chain):
            return []
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
            strike = self._c.safe_float(self._c.safe_get(row, "option_strike_price", default=0))
            exp = str(self._c.safe_get(row, "option_expiry_date_distance", "strike_time", default=""))
            try:
                expiry = datetime.strptime(exp[:10], "%Y-%m-%d").date()
            except (ValueError, TypeError):
                continue
            bid = self._c.safe_float(self._c.safe_get(row, "bid_price", default=0))
            ask = self._c.safe_float(self._c.safe_get(row, "ask_price", default=0))
            mid = (bid + ask) / 2 if (bid and ask) else self._c.safe_float(
                self._c.safe_get(row, "last_price", default=0))
            delta = self._c.safe_float(self._c.safe_get(row, "option_delta", default=0))
            if strike <= 0 or mid <= 0:
                continue
            out.append(OptionQuote(code=code, underlying=underlying, expiry=expiry,
                                   strike=strike, right=str(right).upper(),
                                   delta=delta, premium=mid))
        return out
```

Note: the exact moomoo snapshot field names for strike/expiry/delta
(`option_strike_price`, `strike_time`, `option_delta`) must be confirmed against
`skills/moomooapi/docs/FIELD_MAPPING.md` and `get_option_chain.py` during the
live smoke test; adjust the `safe_get` keys if the SDK names differ. `safe_get`
already tolerates multiple candidate keys.

- [ ] **Step 2: Verify offline suite still imports/passes**

Run: `python3 -m pytest -q`
Expected: PASS — `moomoo_broker.py` only imports `moomoo` lazily inside methods,
so the module still imports without the SDK and no offline test touches the new
method.

- [ ] **Step 3: Add `overlay` to the published webhook schema**

In `docs/webhook-payload.schema.json`, inside `$defs.SignalChange.properties`
(after the `driver` property, line ~63), add:

```json
        "overlay": {
          "anyOf": [
            {"enum": ["COVERED_CALL", "PROTECTIVE_PUT", "COLLAR",
                      "CALL_DIAGONAL", "BEAR_PUT_SPREAD"]},
            {"type": "null"}
          ],
          "default": null,
          "title": "Overlay",
          "description": "Optional option-overlay intent; absent/null = plain equity. Contract (strike/expiry) is chosen locally by the trader (delta+DTE)."
        }
```

Update the top-level `description` to note the new field (append one sentence):

```
... confidence = |points_delta|/10 clamped to [0,1]. Optional per-change `overlay` selects an option strategy; the trader resolves the concrete contract locally.
```

- [ ] **Step 4: Validate the JSON parses**

Run: `python3 -c "import json; json.load(open('docs/webhook-payload.schema.json')); print('ok')"`
Expected: `ok`

- [ ] **Step 5: Commit**

```bash
git add autotrader/moomoo_broker.py docs/webhook-payload.schema.json
git commit -m "feat(broker): live option chain fetch + publish overlay in webhook schema"
```

---

## Task 11: Documentation + suite gate

**Files:**
- Modify: `config/risk.config.example` (if present) or note env vars in `RUNBOOK.md`
- Reference: `CLAUDE.md`

- [ ] **Step 1: Check for a risk config example file**

Run: `ls config/ && grep -rn "RISK_ALLOWED_SYMBOLS" config/ RUNBOOK.md 2>/dev/null | head`
Expected: shows where risk env vars are documented (a `risk.config.example` or RUNBOOK section).

- [ ] **Step 2: Document the new (default-off) option env vars**

Add to whichever file documents `RISK_*` vars (example block — append, do not
change existing values):

```bash
# --- Options overlays (DEFAULT OFF; HUMAN-REVIEW values) ---
# Leave RISK_ALLOWED_OVERLAYS empty and RISK_MAX_OPTION_CONTRACTS=0 to keep options disabled.
RISK_ALLOWED_OVERLAYS=                 # e.g. COVERED_CALL,PROTECTIVE_PUT
RISK_MAX_OPTION_CONTRACTS=0            # per-leg contract cap (0 = options off)
RISK_MAX_OPTION_PREMIUM_PER_TRADE=0    # max debit ($) per long leg
RISK_OPTION_TARGET_DELTA=0.30          # contract-selection delta target
RISK_OPTION_DTE_MIN=30
RISK_OPTION_DTE_MAX=45
RISK_OPTION_DTE_TO_CLOSE=7             # exit declaration (enforced in O4)
RISK_OPTION_PROFIT_TARGET_PCT=0.5      # exit declaration (enforced in O4)
```

- [ ] **Step 3: Run the full suite as the final gate**

Run: `python3 -m pytest -q`
Expected: PASS (all green — equity + new options tests).

- [ ] **Step 4: Commit**

```bash
git add -A
git commit -m "docs(options): document default-off option overlay env vars (O1)"
```

---

## Self-Review Notes (for the implementer)

- **Default-off invariant:** with no `RISK_ALLOWED_OVERLAYS` and
  `RISK_MAX_OPTION_CONTRACTS=0`, every overlay either skips
  (`SKIP_OVERLAY_DISABLED`) or is rejected by the risk core. Production behavior
  is unchanged until a human edits the config.
- **No naked short:** a short OPEN leg is approved only when held underlying
  shares ≥ `qty × multiplier` (Task 6). Covered call is the only short overlay
  in O1.
- **Single audited path:** overlay legs go through `OrderRouter.submit` exactly
  like equities — same audit-first guarantee (Task 9).
- **Backward compatibility:** `overlay` is optional everywhere; equity payloads
  and the equity tick path are untouched (`test_equity_signal_unaffected`,
  `test_no_overlay_is_equity`).
- **Deferred to later O-phases (explicitly out of O1 scope):** multi-leg
  coordination + unwind (O2), spreads/diagonals (O3), exit *execution* /
  daily-premium aggregation / gross-exposure aggregation across legs (O4). O1
  declares the exit rule but does not act on it.
- **Live-path caveat:** `MoomooBroker.get_option_chain` field names need
  confirming against the vendored SDK docs during the live paper smoke test
  (Task 10).
