# Portfolio Rebalancing + Midday Risk Re-check Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add signal-score-driven portfolio rebalancing (target weights + drift bands), a tiered midday risk re-check, and trailing-stop consolidation on position-qty changes — all paper-only, through the existing single audited order path.

**Architecture:** Three new pure-compute units (`rebalance.py`, `risk_check.py`, plus score→fraction math) feed effects executed by `TradeEngine`. Target weights ride an optional `portfolio_targets[]` block on the existing `RoutineSignalPayload` (no new ingress), are persisted in a new `target_weights` DB table, and drive a midday `REBALANCE` job. Two `RISK_CHECK` jobs run a tiered gate→flatten+halt. Every rebalance/flatten/stop order goes through `risk_core.evaluate → OrderRouter.submit → db.record_trade`.

**Tech Stack:** Python 3.14, stdlib + pydantic v2 (schema only), pytest. No moomoo SDK in any new core module. SimBroker + FixedClock for deterministic tests.

**Spec:** `docs/superpowers/specs/2026-06-16-portfolio-rebalancing-design.md`

**Conventions in this repo (read before starting):**
- Tests live in `tests/test_*.py`, run with `python3 -m pytest`.
- `RiskConfig` (`autotrader/config.py`) is a frozen dataclass; new limits are added as fields **with defaults** so an un-updated `config/risk.config` still loads.
- `risk_core.evaluate(req, snapshot, cfg, ref_price)` is the ONLY approval gate; new order paths call it, never bypass it.
- `OrderRouter.make_client_order_id(symbol, side, qty, signal_id)` builds deterministic, idempotent cids.
- The frozen domain types are in `autotrader/domain.py` (`OrderRequest`, `AccountSnapshot`, `Position`, `OrderState`, `TickResult` lives in `main.py`).

---

### Task 1: Carry target weights on the signal payload

**Files:**
- Modify: `autotrader/signals/schema.py`
- Test: `tests/test_signal_schema_targets.py` (create)

- [ ] **Step 1: Write the failing test**

```python
# tests/test_signal_schema_targets.py
from autotrader.signals.schema import RoutineSignalPayload, TargetWeight


def test_payload_without_targets_defaults_empty():
    p = RoutineSignalPayload.model_validate({
        "routine_id": "r1", "timestamp": "2026-06-16T12:00:00Z",
        "signal_changes": [],
    })
    assert p.portfolio_targets == []


def test_payload_parses_portfolio_targets():
    p = RoutineSignalPayload.model_validate({
        "routine_id": "r1", "timestamp": "2026-06-16T12:00:00Z",
        "signal_changes": [],
        "portfolio_targets": [
            {"symbol": "US.AAPL", "score": 80.0},
            {"symbol": "US.MSFT", "score": 60.0},
        ],
    })
    assert [t.symbol for t in p.portfolio_targets] == ["US.AAPL", "US.MSFT"]
    assert isinstance(p.portfolio_targets[0], TargetWeight)
    assert p.portfolio_targets[1].score == 60.0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_signal_schema_targets.py -v`
Expected: FAIL — `ImportError: cannot import name 'TargetWeight'`

- [ ] **Step 3: Add the model + field**

In `autotrader/signals/schema.py`, add a `TargetWeight` model after `Catalyst` and add the field to `RoutineSignalPayload`:

```python
class TargetWeight(BaseModel):
    symbol: str
    score: float


class RoutineSignalPayload(BaseModel):
    routine_id: str
    timestamp: datetime
    signal_changes: List[SignalChange]
    # Validated and carried, but NOT executed in 2c (hard-stop execution and
    # catalyst logic are later phases — YAGNI).
    hard_stops: Dict[str, float] = Field(default_factory=dict)
    catalysts: List[Catalyst] = Field(default_factory=list)
    # Signal-score portfolio targets for the midday rebalancer (raw composite
    # scores; renormalized to weights at rebalance time). Optional — a payload
    # with only signal_changes is unchanged.
    portfolio_targets: List[TargetWeight] = Field(default_factory=list)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/test_signal_schema_targets.py -v`
Expected: PASS (2 passed)

- [ ] **Step 5: Commit**

```bash
git add autotrader/signals/schema.py tests/test_signal_schema_targets.py
git commit -m "feat(signals): carry optional portfolio_targets on RoutineSignalPayload"
```

---

### Task 2: Add rebalance + risk config fields

**Files:**
- Modify: `autotrader/config.py`
- Test: `tests/test_config_rebalance.py` (create)

- [ ] **Step 1: Write the failing test**

```python
# tests/test_config_rebalance.py
import pytest

from autotrader.config import load_risk_config


def _base_env(monkeypatch):
    monkeypatch.setenv("RISK_ALLOWED_SYMBOLS", "US.AAPL,US.MSFT")
    monkeypatch.setenv("RISK_DAILY_LOSS_LIMIT", "500")


def test_defaults_when_unset(monkeypatch):
    _base_env(monkeypatch)
    for k in ("RISK_REBALANCE_ENABLED", "RISK_REBALANCE_BAND_PCT",
              "RISK_REBALANCE_MIN_NOTIONAL", "RISK_REBALANCE_CASH_BUFFER_PCT",
              "RISK_TARGET_STALENESS_HOURS", "RISK_DAILY_LOSS_HALT"):
        monkeypatch.delenv(k, raising=False)
    cfg = load_risk_config()
    assert cfg.rebalance_enabled is False
    assert cfg.rebalance_band_pct == 5.0
    assert cfg.rebalance_min_notional == 200.0
    assert cfg.rebalance_cash_buffer_pct == 10.0
    assert cfg.target_staleness_hours == 24.0
    assert cfg.daily_loss_halt == 1000.0


def test_enabled_parsed_from_env(monkeypatch):
    _base_env(monkeypatch)
    monkeypatch.setenv("RISK_REBALANCE_ENABLED", "true")
    assert load_risk_config().rebalance_enabled is True


def test_halt_must_exceed_soft_limit(monkeypatch):
    _base_env(monkeypatch)
    monkeypatch.setenv("RISK_DAILY_LOSS_LIMIT", "500")
    monkeypatch.setenv("RISK_DAILY_LOSS_HALT", "400")  # <= soft
    with pytest.raises(ValueError, match="RISK_DAILY_LOSS_HALT"):
        load_risk_config()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_config_rebalance.py -v`
Expected: FAIL — `AttributeError: 'RiskConfig' object has no attribute 'rebalance_enabled'`

- [ ] **Step 3: Add fields, a bool helper, loading, and validation**

In `autotrader/config.py`, add fields to the dataclass (after `confidence_size_ceil`):

```python
    # Portfolio rebalancing (additive; rebalance_enabled default off). Drift band
    # is in percentage POINTS of weight; min_notional skips churn; cash_buffer is
    # reserved off investable equity; staleness skips stale target snapshots.
    rebalance_enabled: bool = False
    rebalance_band_pct: float = 5.0
    rebalance_min_notional: float = 200.0
    rebalance_cash_buffer_pct: float = 10.0
    target_staleness_hours: float = 24.0
    # Hard daily-loss flatten+halt threshold (must exceed the soft daily_loss_limit,
    # which gates new entries). Both are positive; breach when day_pnl <= -value.
    daily_loss_halt: float = 1000.0
```

Add a bool env helper next to `_f`:

```python
def _b(name: str, default: bool) -> bool:
    return os.getenv(name, str(default)).strip().lower() in ("1", "true", "yes", "on")
```

In `load_risk_config`, build the new fields and validate before returning. Replace the `return RiskConfig(...)` block with:

```python
    daily_loss_limit = _f("RISK_DAILY_LOSS_LIMIT", 500)
    daily_loss_halt = _f("RISK_DAILY_LOSS_HALT", 1000)
    if daily_loss_halt <= daily_loss_limit:
        raise ValueError(
            f"RISK_DAILY_LOSS_HALT ({daily_loss_halt}) must exceed "
            f"RISK_DAILY_LOSS_LIMIT ({daily_loss_limit})")
    return RiskConfig(
        trading_env=env,
        min_confidence=_f("RISK_MIN_CONFIDENCE", 0.6),
        max_order_notional=_f("RISK_MAX_ORDER_NOTIONAL", 2000),
        max_position_qty=int(_f("RISK_MAX_POSITION_QTY", 100)),
        daily_loss_limit=daily_loss_limit,
        max_gross_exposure=_f("RISK_MAX_GROSS_EXPOSURE", 50000),
        allowed_symbols=symbols,
        trailing_stop_pct=_f("RISK_TRAILING_STOP_PCT", 0.0),
        risk_per_trade_pct=_f("RISK_PER_TRADE_PCT", 0.0),
        confidence_size_floor=_f("RISK_CONFIDENCE_SIZE_FLOOR", 0.5),
        confidence_size_ceil=_f("RISK_CONFIDENCE_SIZE_CEIL", 1.0),
        rebalance_enabled=_b("RISK_REBALANCE_ENABLED", False),
        rebalance_band_pct=_f("RISK_REBALANCE_BAND_PCT", 5.0),
        rebalance_min_notional=_f("RISK_REBALANCE_MIN_NOTIONAL", 200.0),
        rebalance_cash_buffer_pct=_f("RISK_REBALANCE_CASH_BUFFER_PCT", 10.0),
        target_staleness_hours=_f("RISK_TARGET_STALENESS_HOURS", 24.0),
        daily_loss_halt=daily_loss_halt,
    )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/test_config_rebalance.py -v`
Expected: PASS (3 passed)

- [ ] **Step 5: Run the full suite to confirm no regressions**

Run: `python3 -m pytest -q`
Expected: PASS (all prior tests still green)

- [ ] **Step 6: Commit**

```bash
git add autotrader/config.py tests/test_config_rebalance.py
git commit -m "feat(config): rebalance + hard-halt risk limits with soft<hard validation"
```

---

### Task 3: DB — target_weights table + open-stop / cancel helpers

**Files:**
- Modify: `autotrader/db.py`
- Test: `tests/test_db_rebalance.py` (create)

- [ ] **Step 1: Write the failing test**

```python
# tests/test_db_rebalance.py
from autotrader.db import DB


def _db(tmp_path):
    return DB(str(tmp_path / "t.db"))


def test_upsert_and_latest_target_weights(tmp_path):
    db = _db(tmp_path)
    assert db.latest_target_weights() is None
    db.upsert_target_weights("2026-06-15", [("US.AAPL", 10.0), ("US.MSFT", 20.0)])
    db.upsert_target_weights("2026-06-16", [("US.AAPL", 80.0), ("US.MSFT", 60.0)])
    as_of, ingested_at, scores = db.latest_target_weights()
    assert as_of == "2026-06-16"
    assert scores == {"US.AAPL": 80.0, "US.MSFT": 60.0}
    assert ingested_at  # ISO timestamp present
    db.close()


def test_upsert_same_date_replaces(tmp_path):
    db = _db(tmp_path)
    db.upsert_target_weights("2026-06-16", [("US.AAPL", 10.0)])
    db.upsert_target_weights("2026-06-16", [("US.AAPL", 99.0)])
    _, _, scores = db.latest_target_weights()
    assert scores == {"US.AAPL": 99.0}
    db.close()


def test_open_trailing_stop_lifecycle(tmp_path):
    db = _db(tmp_path)
    assert db.get_open_trailing_stop("US.AAPL") is None
    db.record_trade(client_order_id="c1", symbol="US.AAPL", side="SELL", qty=5,
                    order_type="TRAILING_STOP", limit_price=None,
                    broker_order_id="b1", state="SUBMITTED")
    assert db.get_open_trailing_stop("US.AAPL") == "b1"
    db.mark_order_cancelled("b1")
    assert db.get_open_trailing_stop("US.AAPL") is None
    db.close()


def test_open_trailing_stop_ignores_non_stop_and_filled(tmp_path):
    db = _db(tmp_path)
    db.record_trade(client_order_id="c2", symbol="US.AAPL", side="BUY", qty=5,
                    order_type="MARKET", limit_price=None,
                    broker_order_id="b2", state="FILLED")
    db.record_trade(client_order_id="c3", symbol="US.AAPL", side="SELL", qty=5,
                    order_type="TRAILING_STOP", limit_price=None,
                    broker_order_id="b3", state="FILLED")
    assert db.get_open_trailing_stop("US.AAPL") is None
    db.close()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_db_rebalance.py -v`
Expected: FAIL — `AttributeError: 'DB' object has no attribute 'upsert_target_weights'`

- [ ] **Step 3: Add the table to `_SCHEMA` and the methods**

In `autotrader/db.py`, append this table to the `_SCHEMA` string (before the closing `"""`):

```sql
CREATE TABLE IF NOT EXISTS target_weights (
    as_of_date  TEXT NOT NULL,
    symbol      TEXT NOT NULL,
    score       REAL NOT NULL,
    ingested_at TEXT NOT NULL,
    PRIMARY KEY (as_of_date, symbol)
);
```

Add methods to the `DB` class (after `resolve_halt`):

```python
    def upsert_target_weights(self, as_of_date: str,
                              rows: List[tuple]) -> None:
        """Replace the target-weight snapshot for as_of_date. rows = [(symbol, score)].
        ingested_at is stamped now (UTC) and drives the rebalance staleness guard."""
        ts = _now()
        with self._lock:
            self._conn.execute("DELETE FROM target_weights WHERE as_of_date=?",
                               (as_of_date,))
            for symbol, score in rows:
                self._conn.execute(
                    "INSERT INTO target_weights (as_of_date,symbol,score,ingested_at) "
                    "VALUES (?,?,?,?)",
                    (as_of_date, symbol.upper(), float(score), ts),
                )
            self._conn.commit()

    def latest_target_weights(self):
        """Return (as_of_date, ingested_at, {symbol: score}) for the newest
        snapshot, or None if none stored."""
        with self._lock:
            row = self._conn.execute(
                "SELECT as_of_date FROM target_weights "
                "ORDER BY as_of_date DESC LIMIT 1").fetchone()
            if row is None:
                return None
            as_of = row[0]
            rows = self._conn.execute(
                "SELECT symbol, score, ingested_at FROM target_weights "
                "WHERE as_of_date=?", (as_of,)).fetchall()
        scores = {r[0]: r[1] for r in rows}
        ingested_at = rows[0][2]
        return as_of, ingested_at, scores

    def get_open_trailing_stop(self, symbol: str):
        """broker_order_id of the most recent working TRAILING_STOP SELL for
        symbol, or None. Working = SUBMITTED/PARTIAL with a broker id."""
        with self._lock:
            row = self._conn.execute(
                "SELECT broker_order_id FROM trades "
                "WHERE symbol=? AND order_type='TRAILING_STOP' AND side='SELL' "
                "AND state IN ('SUBMITTED','PARTIAL') AND broker_order_id IS NOT NULL "
                "ORDER BY id DESC LIMIT 1", (symbol,)).fetchone()
        return row[0] if row else None

    def mark_order_cancelled(self, broker_order_id: str) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE trades SET state='CANCELLED' WHERE broker_order_id=?",
                (broker_order_id,))
            self._conn.commit()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/test_db_rebalance.py -v`
Expected: PASS (4 passed)

- [ ] **Step 5: Commit**

```bash
git add autotrader/db.py tests/test_db_rebalance.py
git commit -m "feat(db): target_weights snapshot + open-trailing-stop / cancel helpers"
```

---

### Task 4: `rebalance.py` — pure target-fraction + drift-band planner

**Files:**
- Create: `autotrader/rebalance.py`
- Test: `tests/test_rebalance.py` (create)

- [ ] **Step 1: Write the failing test**

```python
# tests/test_rebalance.py
from autotrader.config import RiskConfig
from autotrader.domain import AccountSnapshot, Position
from autotrader.rebalance import compute_plan, target_fractions


def _cfg(**kw):
    base = dict(
        trading_env="PAPER", min_confidence=0.6, max_order_notional=1e9,
        max_position_qty=10_000, daily_loss_limit=500, max_gross_exposure=1e9,
        allowed_symbols=frozenset({"US.AAPL", "US.MSFT"}),
        rebalance_band_pct=5.0, rebalance_min_notional=200.0,
        rebalance_cash_buffer_pct=10.0,
    )
    base.update(kw)
    return RiskConfig(**base)


def test_target_fractions_renormalize_and_buffer():
    # scores 30/10 -> 0.75/0.25 of investable; investable = 1 - 0.10 buffer
    fr = target_fractions({"US.AAPL": 30.0, "US.MSFT": 10.0},
                          allowed=frozenset({"US.AAPL", "US.MSFT"}),
                          cash_buffer_pct=10.0)
    assert abs(fr["US.AAPL"] - 0.675) < 1e-9   # 0.75 * 0.90
    assert abs(fr["US.MSFT"] - 0.225) < 1e-9   # 0.25 * 0.90


def test_within_band_is_skipped():
    # AAPL target weight ~0.45 of 10000 = 4500; hold 45 @ 100 = 4500 -> 0 drift
    snap = AccountSnapshot(cash=5500.0, total_assets=10000.0, day_pnl=0.0,
                           stale=False, positions=(Position("US.AAPL", 45, 100.0),))
    plan = compute_plan(snap, {"US.AAPL": 100.0}, {"US.AAPL": 100.0}, _cfg())
    assert plan.trades == ()
    assert ("US.AAPL", "WITHIN_BAND") in plan.skipped


def test_overweight_trims_partial_sell():
    # target 0.45*10000 = 4500; hold 80 @ 100 = 8000 -> trim ~3500/100 = 35 shares
    snap = AccountSnapshot(cash=2000.0, total_assets=10000.0, day_pnl=0.0,
                           stale=False, positions=(Position("US.AAPL", 80, 100.0),))
    plan = compute_plan(snap, {"US.AAPL": 100.0}, {"US.AAPL": 100.0}, _cfg())
    assert len(plan.trades) == 1
    t = plan.trades[0]
    assert (t.symbol, t.side, t.action) == ("US.AAPL", "SELL", "TRIM")
    assert t.qty == 35 and t.new_total_qty == 45


def test_underweight_tops_up_buy():
    # target 4500; hold 10 @ 100 = 1000 -> top up 3500/100 = 35 shares
    snap = AccountSnapshot(cash=9000.0, total_assets=10000.0, day_pnl=0.0,
                           stale=False, positions=(Position("US.AAPL", 10, 100.0),))
    plan = compute_plan(snap, {"US.AAPL": 100.0}, {"US.AAPL": 100.0}, _cfg())
    t = plan.trades[0]
    assert (t.symbol, t.side, t.action) == ("US.AAPL", "BUY", "TOPUP")
    assert t.qty == 35 and t.new_total_qty == 45


def test_min_notional_skips_churn():
    # drift just over band but tiny dollar value -> skipped
    snap = AccountSnapshot(cash=4490.0, total_assets=10000.0, day_pnl=0.0,
                           stale=False, positions=(Position("US.AAPL", 55, 100.0),))
    cfg = _cfg(rebalance_min_notional=2000.0)
    plan = compute_plan(snap, {"US.AAPL": 100.0}, {"US.AAPL": 100.0}, cfg)
    assert plan.trades == ()
    assert ("US.AAPL", "SKIPPED_MIN_NOTIONAL") in plan.skipped


def test_untargeted_position_left_alone():
    snap = AccountSnapshot(cash=0.0, total_assets=10000.0, day_pnl=0.0, stale=False,
                           positions=(Position("US.NIO", 100, 50.0),))
    plan = compute_plan(snap, {"US.AAPL": 100.0}, {"US.AAPL": 100.0}, _cfg())
    assert all(t.symbol != "US.NIO" for t in plan.trades)


def test_trims_ordered_before_topups():
    # AAPL overweight (trim), MSFT underweight (top-up); trims must come first
    snap = AccountSnapshot(cash=1000.0, total_assets=10000.0, day_pnl=0.0,
                           stale=False,
                           positions=(Position("US.AAPL", 80, 100.0),
                                      Position("US.MSFT", 10, 100.0)))
    plan = compute_plan(snap, {"US.AAPL": 100.0, "US.MSFT": 100.0},
                        {"US.AAPL": 100.0, "US.MSFT": 100.0}, _cfg())
    actions = [t.action for t in plan.trades]
    assert actions.index("TRIM") < actions.index("TOPUP")


def test_missing_price_symbol_skipped():
    snap = AccountSnapshot(cash=10000.0, total_assets=10000.0, day_pnl=0.0,
                           stale=False, positions=())
    plan = compute_plan(snap, {"US.AAPL": 100.0}, {}, _cfg())  # no price
    assert plan.trades == ()
    assert ("US.AAPL", "NO_PRICE") in plan.skipped
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_rebalance.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'autotrader.rebalance'`

- [ ] **Step 3: Implement the module**

```python
# autotrader/rebalance.py
"""Pure portfolio rebalancer. No broker/SDK/clock: (snapshot, scores, prices,
cfg) -> RebalancePlan. The engine executes the plan through the audited risk
path. Two-sided: overweight positions trim (partial SELL), underweight ones top
up (BUY); trims are ordered first so cash frees up before top-ups. Positions
with no target are left untouched (managed by their strategy/trailing stop)."""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Dict, FrozenSet, Tuple

from autotrader.config import RiskConfig
from autotrader.domain import AccountSnapshot


@dataclass(frozen=True)
class RebalanceTrade:
    symbol: str
    side: str          # "BUY" | "SELL"
    qty: int
    action: str        # "TRIM" | "TOPUP"
    new_total_qty: int  # intended post-trade position qty (for stop consolidation)


@dataclass(frozen=True)
class RebalancePlan:
    trades: Tuple[RebalanceTrade, ...]
    skipped: Tuple[Tuple[str, str], ...]   # (symbol, reason) for logging


def target_fractions(scores: Dict[str, float], allowed: FrozenSet[str],
                     cash_buffer_pct: float) -> Dict[str, float]:
    """Renormalize positive scores (restricted to the allow-list) to fractions of
    INVESTABLE equity, where investable = 1 - cash_buffer. Returns {} if no
    positive score remains."""
    pos = {s.upper(): v for s, v in scores.items()
           if s.upper() in allowed and v > 0}
    total = sum(pos.values())
    if total <= 0:
        return {}
    investable = max(0.0, 1.0 - cash_buffer_pct / 100.0)
    return {s: (v / total) * investable for s, v in pos.items()}


def compute_plan(snapshot: AccountSnapshot, scores: Dict[str, float],
                 prices: Dict[str, float], cfg: RiskConfig) -> RebalancePlan:
    fractions = target_fractions(scores, cfg.allowed_symbols,
                                 cfg.rebalance_cash_buffer_pct)
    total = snapshot.total_assets
    band = cfg.rebalance_band_pct / 100.0
    trims: list = []
    topups: list = []
    skipped: list = []
    if total <= 0:
        return RebalancePlan((), tuple((s, "NO_EQUITY") for s in fractions))

    for symbol in sorted(fractions):
        target_weight = fractions[symbol]
        price = prices.get(symbol)
        if price is None or not math.isfinite(price) or price <= 0:
            skipped.append((symbol, "NO_PRICE"))
            continue
        current_qty = snapshot.position_qty(symbol)
        current_value = current_qty * price
        current_weight = current_value / total
        drift = current_weight - target_weight
        if abs(drift) <= band:
            skipped.append((symbol, "WITHIN_BAND"))
            continue
        target_value = target_weight * total
        if drift > band:  # overweight -> trim
            qty = int((current_value - target_value) // price)
            qty = min(qty, current_qty)
            if qty <= 0:
                skipped.append((symbol, "WITHIN_BAND"))
                continue
            if qty * price < cfg.rebalance_min_notional:
                skipped.append((symbol, "SKIPPED_MIN_NOTIONAL"))
                continue
            trims.append(RebalanceTrade(symbol, "SELL", qty, "TRIM",
                                        current_qty - qty))
        else:              # underweight -> top up
            qty = int((target_value - current_value) // price)
            if qty <= 0:
                skipped.append((symbol, "WITHIN_BAND"))
                continue
            if qty * price < cfg.rebalance_min_notional:
                skipped.append((symbol, "SKIPPED_MIN_NOTIONAL"))
                continue
            topups.append(RebalanceTrade(symbol, "BUY", qty, "TOPUP",
                                         current_qty + qty))

    return RebalancePlan(tuple(trims) + tuple(topups), tuple(skipped))
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/test_rebalance.py -v`
Expected: PASS (8 passed)

- [ ] **Step 5: Commit**

```bash
git add autotrader/rebalance.py tests/test_rebalance.py
git commit -m "feat(rebalance): pure target-fraction + drift-band planner"
```

---

### Task 5: `risk_check.py` — pure tiered risk evaluation

**Files:**
- Create: `autotrader/risk_check.py`
- Test: `tests/test_risk_check.py` (create)

- [ ] **Step 1: Write the failing test**

```python
# tests/test_risk_check.py
from autotrader.config import RiskConfig
from autotrader.domain import AccountSnapshot
from autotrader.risk_check import evaluate, RiskAction


def _cfg():
    return RiskConfig(
        trading_env="PAPER", min_confidence=0.6, max_order_notional=2000,
        max_position_qty=100, daily_loss_limit=500, max_gross_exposure=50000,
        allowed_symbols=frozenset({"US.AAPL"}), daily_loss_halt=1000)


def _snap(day_pnl):
    return AccountSnapshot(cash=0.0, total_assets=0.0, day_pnl=day_pnl,
                           stale=False, positions=())


def test_ok_when_no_breach():
    assert evaluate(_snap(-100.0), _cfg()) is RiskAction.OK


def test_gate_on_soft_breach():
    assert evaluate(_snap(-600.0), _cfg()) is RiskAction.GATE


def test_halt_on_hard_breach():
    assert evaluate(_snap(-1200.0), _cfg()) is RiskAction.HALT


def test_boundary_soft_inclusive():
    assert evaluate(_snap(-500.0), _cfg()) is RiskAction.GATE


def test_boundary_hard_inclusive():
    assert evaluate(_snap(-1000.0), _cfg()) is RiskAction.HALT
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_risk_check.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'autotrader.risk_check'`

- [ ] **Step 3: Implement the module**

```python
# autotrader/risk_check.py
"""Pure tiered intraday risk evaluation. No I/O, no SDK. day_pnl is negative when
losing. HALT (hard) is checked before GATE (soft) so the worse breach wins. The
engine applies the effect: GATE closes the entry gate; HALT flattens + halts."""
from __future__ import annotations

import enum

from autotrader.config import RiskConfig
from autotrader.domain import AccountSnapshot


class RiskAction(enum.Enum):
    OK = "OK"
    GATE = "GATE"     # close entry gate (no new BUYs); keep positions + stops
    HALT = "HALT"     # flatten all + cancel all + halt for the day


def evaluate(snapshot: AccountSnapshot, cfg: RiskConfig) -> RiskAction:
    if snapshot.day_pnl <= -abs(cfg.daily_loss_halt):
        return RiskAction.HALT
    if snapshot.day_pnl <= -abs(cfg.daily_loss_limit):
        return RiskAction.GATE
    return RiskAction.OK
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/test_risk_check.py -v`
Expected: PASS (5 passed)

- [ ] **Step 5: Commit**

```bash
git add autotrader/risk_check.py tests/test_risk_check.py
git commit -m "feat(risk): pure tiered intraday risk evaluation (OK/GATE/HALT)"
```

---

### Task 6: Session halt on the entry gate

**Files:**
- Modify: `autotrader/lifecycle.py`
- Test: `tests/test_entry_gate_halt.py` (create)

- [ ] **Step 1: Write the failing test**

```python
# tests/test_entry_gate_halt.py
from autotrader.lifecycle import EntryGate


def test_defaults_not_halted():
    g = EntryGate()
    assert g.halted is False


def test_halt_sets_flag_and_closes_entries():
    g = EntryGate(enabled=True)
    g.halt()
    assert g.halted is True
    assert g.entries_enabled is False  # halting also closes entries


def test_open_does_not_reenable_after_halt():
    g = EntryGate(enabled=False)
    g.halt()
    g.open()
    assert g.entries_enabled is False  # a halted session cannot re-open entries
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_entry_gate_halt.py -v`
Expected: FAIL — `AttributeError: 'EntryGate' object has no attribute 'halted'`

- [ ] **Step 3: Extend `EntryGate`**

Replace the `EntryGate` class body in `autotrader/lifecycle.py`:

```python
class EntryGate:
    """Whether new BUY entries are currently permitted (defaults closed), plus a
    one-way session HALT. Once halted, entries can never re-open this session and
    the runner/engine stop trading."""

    def __init__(self, enabled: bool = False):
        self._enabled = enabled
        self._halted = False

    @property
    def entries_enabled(self) -> bool:
        return self._enabled and not self._halted

    @property
    def halted(self) -> bool:
        return self._halted

    def open(self) -> None:
        self._enabled = True

    def close(self) -> None:
        self._enabled = False

    def halt(self) -> None:
        self._halted = True
        self._enabled = False
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/test_entry_gate_halt.py -v`
Expected: PASS (3 passed)

- [ ] **Step 5: Run the full suite (EntryGate is widely used)**

Run: `python3 -m pytest -q`
Expected: PASS (no regressions)

- [ ] **Step 6: Commit**

```bash
git add autotrader/lifecycle.py tests/test_entry_gate_halt.py
git commit -m "feat(lifecycle): one-way session halt on EntryGate"
```

---

### Task 7: Engine — audited rebalance submit + stop consolidation

**Files:**
- Modify: `autotrader/main.py` (`TradeEngine`)
- Test: `tests/test_engine_rebalance_submit.py` (create)

- [ ] **Step 1: Write the failing test**

```python
# tests/test_engine_rebalance_submit.py
from autotrader.config import RiskConfig
from autotrader.db import DB
from autotrader.lifecycle import EntryGate
from autotrader.main import TradeEngine
from autotrader.rebalance import RebalanceTrade
from autotrader.sim_broker import SimBroker
from autotrader.strategies.threshold import StrategyParams, ThresholdStrategy


def _cfg(**kw):
    base = dict(trading_env="PAPER", min_confidence=0.6, max_order_notional=1e9,
                max_position_qty=10_000, daily_loss_limit=500,
                max_gross_exposure=1e9, allowed_symbols=frozenset({"US.AAPL"}),
                trailing_stop_pct=5.0)
    base.update(kw)
    return RiskConfig(**base)


def _engine(tmp_path, broker, cfg, gate):
    strat = ThresholdStrategy(StrategyParams("US.AAPL", 1.0, 0.05, 0.10, 0.7))
    db = DB(str(tmp_path / "t.db"))
    eng = TradeEngine(broker, strat, cfg, order_qty=1,
                      audit_path=str(tmp_path / "audit.jsonl"), db=db,
                      entry_gate=gate)
    return eng, db


def test_partial_sell_routes_through_risk_and_records(tmp_path):
    broker = SimBroker({"US.AAPL": 100.0})
    gate = EntryGate(enabled=True)
    eng, db = _engine(tmp_path, broker, _cfg(), gate)
    # seed a position to sell from
    from autotrader.domain import OrderRequest
    broker.place_order(OrderRequest("US.AAPL", "BUY", 80, "MARKET", None, "seed"))
    trade = RebalanceTrade("US.AAPL", "SELL", 35, "TRIM", 45)
    res = eng.submit_rebalance_order(trade, ref_price=100.0, round_id="rbal-x")
    assert res.action == "ORDER_PLACED"
    row = db._conn.execute(
        "SELECT side, qty, order_type FROM trades WHERE qty=35").fetchone()
    assert row == ("SELL", 35, "MARKET")
    db.close()


def test_topup_blocked_when_gate_closed(tmp_path):
    broker = SimBroker({"US.AAPL": 100.0})
    gate = EntryGate(enabled=False)  # entries closed
    eng, db = _engine(tmp_path, broker, _cfg(), gate)
    trade = RebalanceTrade("US.AAPL", "BUY", 10, "TOPUP", 10)
    res = eng.submit_rebalance_order(trade, ref_price=100.0, round_id="rbal-x")
    assert res.action == "ENTRY_CLOSED"
    db.close()


def test_submit_blocked_when_halted(tmp_path):
    broker = SimBroker({"US.AAPL": 100.0})
    gate = EntryGate(enabled=True)
    gate.halt()
    eng, db = _engine(tmp_path, broker, _cfg(), gate)
    trade = RebalanceTrade("US.AAPL", "SELL", 5, "TRIM", 0)
    res = eng.submit_rebalance_order(trade, ref_price=100.0, round_id="rbal-x")
    assert res.action == "HALTED"
    db.close()


def test_consolidate_stop_replaces_old_stop(tmp_path):
    broker = SimBroker({"US.AAPL": 100.0})
    gate = EntryGate(enabled=True)
    eng, db = _engine(tmp_path, broker, _cfg(), gate)
    from autotrader.domain import OrderRequest
    # position of 45 shares so a SELL-45 trailing stop passes the long-only check
    broker.place_order(OrderRequest("US.AAPL", "BUY", 45, "MARKET", None, "seed"))
    # existing stop for 10 shares
    eng.consolidate_stop("US.AAPL", new_total_qty=10, ref_price=100.0, round_id="r1")
    first = db.get_open_trailing_stop("US.AAPL")
    assert first is not None
    # consolidate to 45 -> old cancelled, new stop present and different
    eng.consolidate_stop("US.AAPL", new_total_qty=45, ref_price=100.0, round_id="r2")
    second = db.get_open_trailing_stop("US.AAPL")
    assert second is not None and second != first
    row = db._conn.execute(
        "SELECT qty FROM trades WHERE broker_order_id=?", (second,)).fetchone()
    assert row[0] == 45
    db.close()


def test_consolidate_to_zero_cancels_without_replacement(tmp_path):
    broker = SimBroker({"US.AAPL": 100.0})
    gate = EntryGate(enabled=True)
    eng, db = _engine(tmp_path, broker, _cfg(), gate)
    from autotrader.domain import OrderRequest
    broker.place_order(OrderRequest("US.AAPL", "BUY", 10, "MARKET", None, "seed"))
    eng.consolidate_stop("US.AAPL", new_total_qty=10, ref_price=100.0, round_id="r1")
    assert db.get_open_trailing_stop("US.AAPL") is not None
    eng.consolidate_stop("US.AAPL", new_total_qty=0, ref_price=100.0, round_id="r2")
    assert db.get_open_trailing_stop("US.AAPL") is None
    db.close()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_engine_rebalance_submit.py -v`
Expected: FAIL — `AttributeError: 'TradeEngine' object has no attribute 'submit_rebalance_order'`

- [ ] **Step 3: Add the two methods to `TradeEngine`**

In `autotrader/main.py`, add these methods to `TradeEngine` (after `_attach_trailing_stop`, before `shutdown`). They reuse `self._router`, `self._b`, `self._cfg`, `self._db`, `self._gate`:

```python
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
        req = OrderRequest(symbol=trade.symbol, side=trade.side, qty=trade.qty,
                           order_type="MARKET", limit_price=None,
                           client_order_id=cid)
        decision = evaluate(req, snap, self._cfg, ref_price=ref_price)
        if not decision.approved:
            logger.warning("rebalance rejected %s %s %d: %s", trade.side,
                           trade.symbol, trade.qty, decision.reason)
            return TickResult("REJECTED_BY_RISK", decision.reason)
        ack = self._router.submit(req)
        if self._db:
            self._db.record_trade(
                client_order_id=ack.client_order_id, symbol=req.symbol,
                side=req.side, qty=req.qty, order_type=req.order_type,
                limit_price=req.limit_price, broker_order_id=ack.broker_order_id,
                state=ack.state.value)
        if ack.state is OrderState.UNKNOWN:
            return TickResult("ORDER_UNKNOWN", ack.client_order_id)
        if ack.state is OrderState.REJECTED:
            return TickResult("ORDER_REJECTED", ack.client_order_id)
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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/test_engine_rebalance_submit.py -v`
Expected: PASS (5 passed)

- [ ] **Step 5: Commit**

```bash
git add autotrader/main.py tests/test_engine_rebalance_submit.py
git commit -m "feat(engine): audited rebalance submit + trailing-stop consolidation"
```

---

### Task 8: Engine — `rebalance(now)` orchestration + tick halt guard

**Files:**
- Modify: `autotrader/main.py` (`TradeEngine`)
- Test: `tests/test_engine_rebalance_run.py` (create)

- [ ] **Step 1: Write the failing test**

```python
# tests/test_engine_rebalance_run.py
from datetime import datetime, timedelta, timezone

from autotrader.config import RiskConfig
from autotrader.db import DB
from autotrader.lifecycle import EntryGate
from autotrader.main import TradeEngine
from autotrader.sim_broker import SimBroker
from autotrader.strategies.threshold import StrategyParams, ThresholdStrategy

NY_NOW = datetime(2026, 6, 16, 12, 30, tzinfo=timezone.utc)


def _cfg(**kw):
    base = dict(trading_env="PAPER", min_confidence=0.6, max_order_notional=1e9,
                max_position_qty=10_000, daily_loss_limit=500,
                max_gross_exposure=1e9, allowed_symbols=frozenset({"US.AAPL"}),
                trailing_stop_pct=5.0, rebalance_enabled=True,
                rebalance_band_pct=5.0, rebalance_min_notional=200.0,
                rebalance_cash_buffer_pct=0.0, target_staleness_hours=24.0)
    base.update(kw)
    return RiskConfig(**base)


def _engine(tmp_path, broker, cfg, gate):
    strat = ThresholdStrategy(StrategyParams("US.AAPL", 1.0, 0.05, 0.10, 0.7))
    db = DB(str(tmp_path / "t.db"))
    eng = TradeEngine(broker, strat, cfg, order_qty=1,
                      audit_path=str(tmp_path / "audit.jsonl"), db=db,
                      entry_gate=gate)
    return eng, db


def test_rebalance_noop_when_disabled(tmp_path):
    broker = SimBroker({"US.AAPL": 100.0})
    eng, db = _engine(tmp_path, broker, _cfg(rebalance_enabled=False),
                      EntryGate(enabled=True))
    db.upsert_target_weights("2026-06-16", [("US.AAPL", 100.0)])
    summary = eng.rebalance(NY_NOW)
    assert summary == "REBALANCE_DISABLED"
    db.close()


def test_rebalance_noop_when_no_targets(tmp_path):
    broker = SimBroker({"US.AAPL": 100.0})
    eng, db = _engine(tmp_path, broker, _cfg(), EntryGate(enabled=True))
    assert eng.rebalance(NY_NOW) == "NO_TARGETS"
    db.close()


def test_rebalance_skips_stale_snapshot(tmp_path):
    broker = SimBroker({"US.AAPL": 100.0})
    eng, db = _engine(tmp_path, broker, _cfg(target_staleness_hours=1.0),
                      EntryGate(enabled=True))
    db.upsert_target_weights("2026-06-16", [("US.AAPL", 100.0)])
    # now is 48h after ingest -> stale
    later = NY_NOW + timedelta(hours=48)
    assert eng.rebalance(later) == "STALE_TARGETS"
    db.close()


def test_rebalance_tops_up_and_attaches_stop(tmp_path):
    broker = SimBroker({"US.AAPL": 100.0}, cash=10000.0)
    gate = EntryGate(enabled=True)
    eng, db = _engine(tmp_path, broker, _cfg(), gate)
    db.upsert_target_weights("2026-06-16", [("US.AAPL", 100.0)])
    summary = eng.rebalance(NY_NOW)
    assert summary == "REBALANCED"
    # bought ~100 shares (10000 / 100) and a consolidated stop exists
    assert broker.get_account().position_qty("US.AAPL") > 0
    assert db.get_open_trailing_stop("US.AAPL") is not None
    db.close()


def test_rebalance_halted_is_noop(tmp_path):
    broker = SimBroker({"US.AAPL": 100.0}, cash=10000.0)
    gate = EntryGate(enabled=True)
    gate.halt()
    eng, db = _engine(tmp_path, broker, _cfg(), gate)
    db.upsert_target_weights("2026-06-16", [("US.AAPL", 100.0)])
    assert eng.rebalance(NY_NOW) == "HALTED"
    assert broker.get_account().position_qty("US.AAPL") == 0
    db.close()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_engine_rebalance_run.py -v`
Expected: FAIL — `AttributeError: 'TradeEngine' object has no attribute 'rebalance'`

- [ ] **Step 3: Add `rebalance(now)` and guard `tick()`**

In `autotrader/main.py`, add the import at the top (with the other `autotrader` imports):

```python
from autotrader.rebalance import compute_plan
```

Add a halt guard as the FIRST line of `TradeEngine.tick`:

```python
    def tick(self) -> TickResult:
        if self._gate is not None and self._gate.halted:
            return TickResult("HALTED")
        snap = self._b.get_account()
        ...
```

Add the `rebalance` method to `TradeEngine` (after `consolidate_stop`):

```python
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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/test_engine_rebalance_run.py -v`
Expected: PASS (5 passed)

- [ ] **Step 5: Run the full suite (tick() changed)**

Run: `python3 -m pytest -q`
Expected: PASS (no regressions)

- [ ] **Step 6: Commit**

```bash
git add autotrader/main.py tests/test_engine_rebalance_run.py
git commit -m "feat(engine): midday rebalance orchestration with staleness + halt guards"
```

---

### Task 8b: risk_core — reduce-only exit exception

**Why:** `risk_core.evaluate` currently rejects *every* order once `day_pnl <= -daily_loss_limit`, including SELLs. The hard-tier flatten (Task 9) and strategy stop-loss exits must always be able to *reduce* a position even during a loss breach. A position-reducing SELL can never increase risk, so it bypasses the three risk-INCREASING caps (daily-loss, order notional, gross exposure) — while env / stale / allow-list / long-only checks still apply. This is a **minimal-diff** change (three guard clauses; no check reordering), so all 14 existing `risk_core` tests stay green.

**Files:**
- Modify: `autotrader/risk_core.py` (`evaluate`)
- Test: `tests/test_risk_core_reduce_only.py` (create)

- [ ] **Step 1: Write the failing test**

```python
# tests/test_risk_core_reduce_only.py
from autotrader.config import RiskConfig
from autotrader.domain import AccountSnapshot, OrderRequest, Position
from autotrader.risk_core import evaluate


def _cfg(**kw):
    base = dict(trading_env="PAPER", min_confidence=0.6, max_order_notional=2000,
                max_position_qty=100, daily_loss_limit=500, max_gross_exposure=50000,
                allowed_symbols=frozenset({"US.AAPL"}), daily_loss_halt=1000)
    base.update(kw)
    return RiskConfig(**base)


def _snap(day_pnl, qty):
    pos = (Position("US.AAPL", qty, 100.0),) if qty else ()
    return AccountSnapshot(cash=0.0, total_assets=qty * 100.0, day_pnl=day_pnl,
                           stale=False, positions=pos)


def _sell(qty):
    return OrderRequest("US.AAPL", "SELL", qty, "MARKET", None, "c1")


def _buy(qty):
    return OrderRequest("US.AAPL", "BUY", qty, "MARKET", None, "c2")


def test_reduce_only_sell_passes_during_loss_breach():
    # day_pnl past the soft limit; selling 40 of 80 held is reduce-only -> allowed
    d = evaluate(_sell(40), _snap(-1200.0, 80), _cfg(), ref_price=100.0)
    assert d.approved, d.reason


def test_full_exit_passes_even_over_notional_cap():
    # 100 @ 100 = 10000 notional >> max_order_notional 2000, but it's an exit
    d = evaluate(_sell(100), _snap(-1200.0, 100), _cfg(), ref_price=100.0)
    assert d.approved, d.reason


def test_buy_still_blocked_during_loss_breach():
    d = evaluate(_buy(1), _snap(-600.0, 0), _cfg(), ref_price=100.0)
    assert not d.approved
    assert "loss" in d.reason.lower()


def test_oversized_sell_that_would_short_is_rejected():
    # selling 120 of 80 held -> resulting -40; not reduce-only; long-only rejects
    d = evaluate(_sell(120), _snap(0.0, 80), _cfg(max_order_notional=1e9),
                 ref_price=100.0)
    assert not d.approved
    assert "long-only" in d.reason


def test_reduce_only_still_blocked_on_stale_snapshot():
    snap = AccountSnapshot(cash=0.0, total_assets=8000.0, day_pnl=-1200.0,
                           stale=True, positions=(Position("US.AAPL", 80, 100.0),))
    d = evaluate(_sell(40), snap, _cfg(), ref_price=100.0)
    assert not d.approved
    assert "stale" in d.reason
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_risk_core_reduce_only.py -v`
Expected: FAIL — `test_reduce_only_sell_passes_during_loss_breach` and `test_full_exit_passes_even_over_notional_cap` fail (currently rejected)

- [ ] **Step 3: Add the reduce-only flag and guard three checks**

Replace the body of `evaluate` in `autotrader/risk_core.py` with this (it preserves the existing check order and rejection messages exactly — only adds the `is_reduce_only` flag and three `not is_reduce_only and` guards):

```python
def evaluate(req: OrderRequest, snapshot: AccountSnapshot, cfg: RiskConfig,
             ref_price: Optional[float]) -> RiskDecision:
    sym = req.symbol.upper()

    # Reduce-only exit: a SELL that strictly lowers an existing long position can
    # never INCREASE risk, so the risk-increasing caps (daily-loss halt, order
    # notional, gross exposure) are skipped for it. Env / stale / allow-list /
    # long-only checks still apply. This lets the loss-halt flatten and strategy
    # stop-loss exits liquidate even while the daily-loss limit is breached.
    held = snapshot.position_qty(sym)
    resulting = held + (req.qty if req.side == "BUY" else -req.qty)
    is_reduce_only = req.side == "SELL" and 0 <= resulting < held

    # 1. Environment routing — v1 is PAPER-only.
    if cfg.trading_env != "PAPER":
        return RiskDecision(False, f"env routing: {cfg.trading_env} not allowed in paper-only v1")

    # 2. Never trade on a stale snapshot.
    if snapshot.stale:
        return RiskDecision(False, "account snapshot is stale; refusing to trade")

    # 3. Daily-loss halt — skipped for reduce-only exits.
    if not is_reduce_only and snapshot.day_pnl <= -abs(cfg.daily_loss_limit):
        return RiskDecision(False, f"daily loss limit breached: pnl={snapshot.day_pnl}")

    # 4. Symbol allow-list.
    if sym not in cfg.allowed_symbols:
        return RiskDecision(False, f"symbol {req.symbol} not in allow-list")

    # 5. A reference price must exist to size/clamp the order.
    eff_price = req.limit_price if (req.order_type == "LIMIT" and req.limit_price) else ref_price
    if eff_price is None or not math.isfinite(eff_price) or eff_price <= 0:
        return RiskDecision(False, "no usable reference price for risk sizing")

    # 6. Max order notional clamp — skipped for reduce-only exits.
    notional = _notional(req, eff_price)
    if not is_reduce_only and (not math.isfinite(notional) or notional > cfg.max_order_notional):
        return RiskDecision(False, f"order notional {notional:.2f} > cap {cfg.max_order_notional}")

    # 7. Resulting position cap. v1 is long-only: a SELL may at most flatten the
    #    held position (resulting >= 0); a negative result is an opening short.
    if resulting < 0:
        return RiskDecision(False, f"long-only: SELL would short position to {resulting}")
    if abs(resulting) > cfg.max_position_qty:
        return RiskDecision(False, f"resulting position {resulting} > cap {cfg.max_position_qty}")

    # 8. Gross exposure cap after this order — skipped for reduce-only exits.
    projected = snapshot.gross_exposure() + notional
    if not is_reduce_only and projected > cfg.max_gross_exposure:
        return RiskDecision(False, f"gross exposure {projected:.2f} > cap {cfg.max_gross_exposure}")

    return RiskDecision(True, "OK")
```

- [ ] **Step 4: Run the new test AND the existing risk_core suite**

Run: `python3 -m pytest tests/test_risk_core_reduce_only.py tests/test_risk_core.py -v`
Expected: PASS — 5 new + all 14 existing green

- [ ] **Step 5: Commit**

```bash
git add autotrader/risk_core.py tests/test_risk_core_reduce_only.py
git commit -m "feat(risk-core): allow position-reducing SELL exits during a loss breach"
```

---

### Task 9: Engine — `apply_risk_check(now)` (gate / flatten+halt)

> Depends on Task 8b: the hard-tier `_flatten_all` SELLs only pass `risk_core` because of the reduce-only exception.

**Files:**
- Modify: `autotrader/main.py` (`TradeEngine`)
- Test: `tests/test_engine_risk_check.py` (create)

- [ ] **Step 1: Write the failing test**

```python
# tests/test_engine_risk_check.py
from datetime import datetime, timezone

from autotrader.config import RiskConfig
from autotrader.db import DB
from autotrader.domain import AccountSnapshot, Position
from autotrader.lifecycle import EntryGate
from autotrader.main import TradeEngine
from autotrader.sim_broker import SimBroker
from autotrader.strategies.threshold import StrategyParams, ThresholdStrategy

NOW = datetime(2026, 6, 16, 13, 30, tzinfo=timezone.utc)


class PnlBroker(SimBroker):
    """SimBroker whose account reports a fixed day_pnl, for risk-check tests."""
    def __init__(self, quotes, day_pnl, **kw):
        super().__init__(quotes, **kw)
        self._day_pnl = day_pnl

    def get_account(self):
        base = super().get_account()
        return AccountSnapshot(cash=base.cash, total_assets=base.total_assets,
                               day_pnl=self._day_pnl, stale=False,
                               positions=base.positions)


def _cfg(**kw):
    base = dict(trading_env="PAPER", min_confidence=0.6, max_order_notional=1e9,
                max_position_qty=10_000, daily_loss_limit=500,
                max_gross_exposure=1e9, allowed_symbols=frozenset({"US.AAPL"}),
                trailing_stop_pct=5.0, daily_loss_halt=1000)
    base.update(kw)
    return RiskConfig(**base)


def _engine(tmp_path, broker, cfg, gate):
    strat = ThresholdStrategy(StrategyParams("US.AAPL", 1.0, 0.05, 0.10, 0.7))
    db = DB(str(tmp_path / "t.db"))
    eng = TradeEngine(broker, strat, cfg, order_qty=1,
                      audit_path=str(tmp_path / "audit.jsonl"), db=db,
                      entry_gate=gate)
    return eng, db


def test_ok_keeps_gate_open(tmp_path):
    broker = PnlBroker({"US.AAPL": 100.0}, day_pnl=-100.0)
    gate = EntryGate(enabled=True)
    eng, db = _engine(tmp_path, broker, _cfg(), gate)
    assert eng.apply_risk_check(NOW) == "OK"
    assert gate.entries_enabled is True
    db.close()


def test_soft_breach_closes_gate_keeps_positions(tmp_path):
    broker = PnlBroker({"US.AAPL": 100.0}, day_pnl=-600.0)
    from autotrader.domain import OrderRequest
    broker.place_order(OrderRequest("US.AAPL", "BUY", 10, "MARKET", None, "seed"))
    gate = EntryGate(enabled=True)
    eng, db = _engine(tmp_path, broker, _cfg(), gate)
    assert eng.apply_risk_check(NOW) == "GATE"
    assert gate.entries_enabled is False
    assert gate.halted is False
    assert broker.get_account().position_qty("US.AAPL") == 10  # not flattened
    db.close()


def test_hard_breach_flattens_and_halts(tmp_path):
    broker = PnlBroker({"US.AAPL": 100.0}, day_pnl=-1200.0)
    from autotrader.domain import OrderRequest
    broker.place_order(OrderRequest("US.AAPL", "BUY", 10, "MARKET", None, "seed"))
    gate = EntryGate(enabled=True)
    eng, db = _engine(tmp_path, broker, _cfg(), gate)
    assert eng.apply_risk_check(NOW) == "HALT"
    assert gate.halted is True
    assert broker.get_account().position_qty("US.AAPL") == 0  # flattened
    halt = db._conn.execute("SELECT reason FROM halts LIMIT 1").fetchone()
    assert halt is not None
    db.close()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_engine_risk_check.py -v`
Expected: FAIL — `AttributeError: 'TradeEngine' object has no attribute 'apply_risk_check'`

- [ ] **Step 3: Add the import, `apply_risk_check`, and `_flatten_all`**

In `autotrader/main.py`, add the import near the top:

```python
from autotrader.risk_check import evaluate as risk_evaluate, RiskAction
```

> Note: `risk_core.evaluate` is already imported as `evaluate`; import the tiered one under an alias to avoid a name clash.

Add these methods to `TradeEngine` (after `rebalance`):

```python
    def _flatten_all(self, snapshot, round_id: str) -> None:
        """Liquidate every long position through the audited path. Called before
        the halt flag is set, so the SELLs are not blocked by the halt guard."""
        from autotrader.rebalance import RebalanceTrade
        for p in snapshot.positions:
            if p.qty <= 0:
                continue
            price = self._b.get_quote(p.symbol) or p.avg_price
            trade = RebalanceTrade(p.symbol, "SELL", p.qty, "TRIM", 0)
            res = self.submit_rebalance_order(trade, price, round_id)
            logger.info("flatten %s qty=%d -> %s", p.symbol, p.qty, res.action)

    def apply_risk_check(self, now) -> str:
        """Tiered intraday preservation. GATE: close entries, keep positions +
        stops. HALT: flatten all, cancel all, record the halt, set the session
        halt flag. Returns the RiskAction name."""
        snap = self._b.get_account()
        action = risk_evaluate(snap, self._cfg)
        if self._db:
            self._db.record_performance(
                day_pnl=snap.day_pnl, total_assets=snap.total_assets,
                cash=snap.cash, gross_exposure=snap.gross_exposure())
        if action is RiskAction.GATE:
            if self._gate is not None:
                self._gate.close()
            logger.warning("RISK_CHECK soft breach: entries closed (pnl=%.2f)",
                           snap.day_pnl)
        elif action is RiskAction.HALT:
            reason = f"daily loss halt: pnl={snap.day_pnl}"
            round_id = f"halt-{now.date().isoformat()}"
            self._flatten_all(snap, round_id)   # BEFORE halt flag (guard would block)
            try:
                self._b.cancel_all()
            except Exception as e:
                logger.error("RISK_CHECK halt: cancel_all failed: %s", e)
            if self._gate is not None:
                self._gate.halt()
            if self._db:
                self._db.record_halt(reason)
            logger.error("RISK_CHECK HARD breach: flattened + halted (%s)", reason)
        return action.value
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/test_engine_risk_check.py -v`
Expected: PASS (3 passed)

- [ ] **Step 5: Commit**

```bash
git add autotrader/main.py tests/test_engine_risk_check.py
git commit -m "feat(engine): tiered midday risk check (gate / flatten+halt)"
```

---

### Task 10: Scheduler — add REBALANCE + two RISK_CHECK jobs

**Files:**
- Modify: `autotrader/scheduler.py`
- Test: `tests/test_scheduler_rebalance.py` (create)

- [ ] **Step 1: Write the failing test**

```python
# tests/test_scheduler_rebalance.py
from datetime import datetime

from autotrader.scheduler import (
    LifecycleScheduler, REBALANCE, RISK_CHECK_MID, RISK_CHECK_LATE,
    PRE_OPEN_SYNC, ENTRY_OPEN, RISK_SWEEP, EOD_FLATTEN,
)


def test_new_jobs_fire_at_their_times():
    s = LifecycleScheduler()
    # 12:35 -> all jobs up to 12:30 are due on first poll (catch-up), in order
    due = s.poll(datetime(2026, 6, 16, 12, 35))
    assert due == [PRE_OPEN_SYNC, ENTRY_OPEN, REBALANCE]


def test_risk_checks_fire_once_each():
    s = LifecycleScheduler()
    assert RISK_CHECK_MID in s.poll(datetime(2026, 6, 16, 13, 30))
    assert s.poll(datetime(2026, 6, 16, 13, 45)) == []   # mid already fired
    assert RISK_CHECK_LATE in s.poll(datetime(2026, 6, 16, 15, 0))


def test_full_day_order():
    s = LifecycleScheduler()
    due = s.poll(datetime(2026, 6, 16, 16, 20))
    assert due == [PRE_OPEN_SYNC, ENTRY_OPEN, REBALANCE, RISK_CHECK_MID,
                   RISK_CHECK_LATE, RISK_SWEEP, EOD_FLATTEN]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/test_scheduler_rebalance.py -v`
Expected: FAIL — `ImportError: cannot import name 'REBALANCE'`

- [ ] **Step 3: Add the job constants and schedule entries**

In `autotrader/scheduler.py`, add constants after `EOD_FLATTEN`:

```python
REBALANCE = "REBALANCE"           # 12:30 — drift-band rebalance + stop consolidation
RISK_CHECK_MID = "RISK_CHECK_MID"   # 13:30 — tiered intraday risk re-check
RISK_CHECK_LATE = "RISK_CHECK_LATE"  # 15:00 — tiered intraday risk re-check
```

Replace `_SCHEDULE` with the chronologically-sorted full set:

```python
_SCHEDULE: Tuple[Tuple[str, time], ...] = (
    (PRE_OPEN_SYNC, time(8, 30)),
    (ENTRY_OPEN, time(9, 45)),
    (REBALANCE, time(12, 30)),
    (RISK_CHECK_MID, time(13, 30)),
    (RISK_CHECK_LATE, time(15, 0)),
    (RISK_SWEEP, time(15, 30)),
    (EOD_FLATTEN, time(16, 15)),
)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/test_scheduler_rebalance.py -v`
Expected: PASS (3 passed)

- [ ] **Step 5: Run the existing scheduler test to confirm no regression**

Run: `python3 -m pytest tests/test_scheduler.py -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add autotrader/scheduler.py tests/test_scheduler_rebalance.py
git commit -m "feat(scheduler): REBALANCE 12:30 + RISK_CHECK 13:30/15:00 jobs"
```

---

### Task 11: Runner — dispatch new jobs, ingest targets, halt guard

**Files:**
- Modify: `autotrader/runner.py`
- Modify: `autotrader/signals/inbox.py` (add `on_targets` callback)
- Modify: `autotrader/main.py` (`main()` wiring only)
- Test: `tests/test_runner_rebalance.py` (create)
- Test: `tests/test_inbox_targets.py` (create)

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_inbox_targets.py
from autotrader.signals.inbox import SignalInbox, atomic_write_bytes


def test_on_targets_called_with_extracted_rows(tmp_path):
    captured = []
    inbox = SignalInbox(str(tmp_path),
                        on_targets=lambda as_of, rows: captured.append((as_of, rows)))
    payload = (
        '{"routine_id":"r1","timestamp":"2026-06-16T12:00:00Z",'
        '"signal_changes":[],'
        '"portfolio_targets":[{"symbol":"US.AAPL","score":80.0},'
        '{"symbol":"US.MSFT","score":60.0}]}'
    )
    atomic_write_bytes(tmp_path, payload.encode())
    inbox.poll()
    assert captured == [("2026-06-16", [("US.AAPL", 80.0), ("US.MSFT", 60.0)])]


def test_no_targets_does_not_call_callback(tmp_path):
    captured = []
    inbox = SignalInbox(str(tmp_path),
                        on_targets=lambda as_of, rows: captured.append(rows))
    payload = ('{"routine_id":"r1","timestamp":"2026-06-16T12:00:00Z",'
               '"signal_changes":[]}')
    atomic_write_bytes(tmp_path, payload.encode())
    inbox.poll()
    assert captured == []
```

```python
# tests/test_runner_rebalance.py
from datetime import datetime

from autotrader.lifecycle import EntryGate
from autotrader.runner import SessionRunner
from autotrader.scheduler import LifecycleScheduler


class _Eng:
    def __init__(self):
        self.calls = []

    def tick(self):
        from autotrader.main import TickResult
        self.calls.append("tick")
        return TickResult("NO_SIGNAL")

    def rebalance(self, now):
        self.calls.append(("rebalance", now))
        return "REBALANCED"

    def apply_risk_check(self, now):
        self.calls.append(("risk", now))
        return "OK"


class _Watch:
    def ensure_healthy(self):
        return True


def _runner(eng, gate):
    return SessionRunner(engine=eng, broker=None, db=None, gate=gate,
                         scheduler=LifecycleScheduler(), watchdog=_Watch(),
                         clock=None, sleep=lambda s: None)


def test_rebalance_and_risk_jobs_dispatch():
    eng, gate = _Eng(), EntryGate(enabled=False)
    runner = _runner(eng, gate)
    runner.run_once(datetime(2026, 6, 16, 16, 20))  # all jobs due
    kinds = [c[0] if isinstance(c, tuple) else c for c in eng.calls]
    assert "rebalance" in kinds
    assert "risk" in kinds


def test_halt_stops_tick():
    eng, gate = _Eng(), EntryGate(enabled=True)
    gate.halt()
    runner = _runner(eng, gate)
    out = runner.run_once(datetime(2026, 6, 16, 9, 50))
    assert out == "HALTED"
    assert "tick" not in eng.calls
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/test_inbox_targets.py tests/test_runner_rebalance.py -v`
Expected: FAIL — `TypeError: __init__() got an unexpected keyword argument 'on_targets'` and runner dispatch assertion errors

- [ ] **Step 3a: Add the `on_targets` callback to `SignalInbox`**

In `autotrader/signals/inbox.py`, update `__init__` and `poll`:

```python
    def __init__(self, inbox_dir: str, confidence_scale: float = 10.0,
                 on_targets=None):
        self._dir = Path(inbox_dir)
        self._processed = self._dir / "processed"
        self._rejected = self._dir / "rejected"
        for d in (self._dir, self._processed, self._rejected):
            d.mkdir(parents=True, exist_ok=True)
        self._scale = confidence_scale
        self._on_targets = on_targets
```

In `poll`, after `payload = RoutineSignalPayload.model_validate_json(...)` and before `signals.extend(...)`, add:

```python
                if payload.portfolio_targets and self._on_targets is not None:
                    self._on_targets(
                        payload.timestamp.date().isoformat(),
                        [(t.symbol.upper(), float(t.score))
                         for t in payload.portfolio_targets])
```

- [ ] **Step 3b: Dispatch new jobs + halt guard in `SessionRunner`**

In `autotrader/runner.py`, update the scheduler import to include the new jobs:

```python
from autotrader.scheduler import (
    LifecycleScheduler, PRE_OPEN_SYNC, ENTRY_OPEN, RISK_SWEEP, EOD_FLATTEN,
    REBALANCE, RISK_CHECK_MID, RISK_CHECK_LATE,
)
```

Change `_run_job` to accept `now` and handle the new jobs (add branches to the existing if/elif chain):

```python
    def _run_job(self, job: str, now) -> None:
        if job == PRE_OPEN_SYNC:
            ground_truth_sync(self._broker, self._db)
        elif job == ENTRY_OPEN:
            self._gate.open()
            logger.info("ENTRY_OPEN: entries enabled")
        elif job == REBALANCE:
            self._engine.rebalance(now)
        elif job in (RISK_CHECK_MID, RISK_CHECK_LATE):
            self._engine.apply_risk_check(now)
        elif job == RISK_SWEEP:
            self._gate.close()
            ground_truth_sync(self._broker, self._db)
            self._record_perf()
            logger.info("RISK_SWEEP: entries closed, ground truth + performance recorded")
        elif job == EOD_FLATTEN:
            self._gate.close()
            self._broker.cancel_all()
            self._record_perf()
            logger.info("EOD_FLATTEN: entries closed, all orders cancelled, performance committed")
```

Update `run_once` to pass `now` to `_run_job` and short-circuit when halted (after jobs run, before the watchdog/tick):

```python
    def run_once(self, now) -> str:
        for job in self._sched.poll(now):
            self._run_job(job, now)
        if self._gate is not None and self._gate.halted:
            return "HALTED"
        if not self._watch.ensure_healthy():
            return "HALTED_UNHEALTHY"
        action = self._engine.tick().action
        if self._inbox is not None:
            for sig in self._inbox.poll():
                res = self._engine.submit_external_signal(sig)
                logger.debug("external signal %s -> %s", sig.symbol, res.action)
        return action
```

- [ ] **Step 3c: Wire `on_targets` in `main()`**

In `autotrader/main.py` `main()`, where the inbox is constructed, pass the DB sink:

```python
    inbox = None
    inbox_dir = os.getenv("AUTOTRADER_SIGNAL_INBOX")
    if inbox_dir:
        from autotrader.signals.inbox import SignalInbox
        inbox = SignalInbox(os.path.expanduser(inbox_dir),
                            on_targets=db.upsert_target_weights)
        logger.info("external-signal inbox at %s", inbox_dir)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/test_inbox_targets.py tests/test_runner_rebalance.py -v`
Expected: PASS (4 passed)

- [ ] **Step 5: Run the full suite (runner + inbox are core)**

Run: `python3 -m pytest -q`
Expected: PASS (no regressions)

- [ ] **Step 6: Commit**

```bash
git add autotrader/runner.py autotrader/signals/inbox.py autotrader/main.py \
        tests/test_inbox_targets.py tests/test_runner_rebalance.py
git commit -m "feat(runner): dispatch rebalance/risk jobs, ingest targets, halt guard"
```

---

### Task 12: Config docs + SDK-free invariant coverage

**Files:**
- Modify: `config/risk.config.example`
- Modify: `tests/test_no_sdk_in_core.py`

- [ ] **Step 1: Add the new vars to the example config**

Append to `config/risk.config.example` (these are human-reviewed risk limits):

```
# --- Portfolio rebalancing (Phase: rebalancing). Disabled by default. -----------
# Midday REBALANCE job (12:30) trims/tops-up positions toward signal-score target
# weights; RISK_CHECK jobs (13:30, 15:00) run a tiered loss preservation gate.
# Risk-limit CHANGES require explicit human review (CLAUDE.md).
RISK_REBALANCE_ENABLED=false
RISK_REBALANCE_BAND_PCT=5.0           # ±percentage-points of weight before acting
RISK_REBALANCE_MIN_NOTIONAL=200       # skip rebalance trades smaller than this
RISK_REBALANCE_CASH_BUFFER_PCT=10.0   # reserve as cash; weights apply to the rest
RISK_TARGET_STALENESS_HOURS=24        # skip rebalance if the target snapshot is older
# Hard daily-loss flatten+halt. MUST exceed the soft RISK_DAILY_LOSS_LIMIT (which
# only closes the entry gate). Breach when day_pnl <= -value.
RISK_DAILY_LOSS_HALT=1000
```

- [ ] **Step 2: Extend the SDK-free test's module tuple**

`tests/test_no_sdk_in_core.py` builds a `code` string that imports a tuple of module names in a subprocess and asserts `moomoo` is not in `sys.modules`. Add the two new modules to that tuple. Change the line:

```python
        "          'autotrader.watchdog', 'autotrader.runner',\n"
```

to:

```python
        "          'autotrader.watchdog', 'autotrader.runner',\n"
        "          'autotrader.rebalance', 'autotrader.risk_check',\n"
```

- [ ] **Step 3: Run the SDK-free test to verify it passes**

Run: `python3 -m pytest tests/test_no_sdk_in_core.py -v`
Expected: PASS (the new modules import stdlib + autotrader only)

- [ ] **Step 4: Run the FULL suite — everything green**

Run: `python3 -m pytest -q`
Expected: PASS (all tests, including the ~12 new files)

- [ ] **Step 5: Commit**

```bash
git add config/risk.config.example tests/test_no_sdk_in_core.py
git commit -m "docs(config): document rebalance/halt limits; cover new core modules as SDK-free"
```

---

## Final verification

- [ ] **Run the complete suite and confirm the count grew with zero failures/skips**

Run: `python3 -m pytest -q`
Expected: PASS — all prior tests plus the new `test_signal_schema_targets`, `test_config_rebalance`, `test_db_rebalance`, `test_rebalance`, `test_risk_check`, `test_entry_gate_halt`, `test_engine_rebalance_submit`, `test_engine_rebalance_run`, `test_risk_core_reduce_only`, `test_engine_risk_check`, `test_scheduler_rebalance`, `test_runner_rebalance`, `test_inbox_targets`.

- [ ] **Confirm no SDK leaked into core**

Run: `python3 -m pytest tests/test_no_sdk_in_core.py -v`
Expected: PASS

- [ ] **Sanity-check the lifecycle ordering by eye**

Open `autotrader/scheduler.py` and confirm `_SCHEDULE` is strictly increasing in time: 08:30, 09:45, 12:30, 13:30, 15:00, 15:30, 16:15. Out-of-order entries would break `poll()`'s chronological contract.

---

## Notes for the implementer

- **Why `risk_evaluate` alias (Task 9):** `main.py` already imports `risk_core.evaluate` as `evaluate`. The tiered `risk_check.evaluate` is imported as `risk_evaluate` to avoid shadowing the risk-core gate that every order still passes through.
- **Stop consolidation sizing (Task 7):** `new_total_qty` is the *intended* post-trade qty passed by the caller, never a re-fetched snapshot — this is the live-async-fill fix from the spec. The RISK eval inside `consolidate_stop` still re-fetches the snapshot (matching `_attach_trailing_stop`), so on live OpenD an unfilled top-up makes the stop fail the long-only check and simply not attach that round, never mis-size.
- **Order of operations on HALT (Task 9):** flatten happens BEFORE `gate.halt()` so the flatten SELLs aren't blocked by `submit_rebalance_order`'s halt guard. Do not reorder.
- **Out of scope (carried from the spec):** replacing the entry flow's `_attach_trailing_stop` with `consolidate_stop`; selective cancel of resting BUY limit orders in the soft tier; any live-trading path; non-linear score→weight models.
```
