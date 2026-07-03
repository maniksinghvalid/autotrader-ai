# Plan: Risk-Per-Trade Position Sizing (confidence-scaled)

## Context

**Why this change.** Today the AutoTrader engine does **no position sizing**. Every BUY
buys a flat `ORDER_QTY` (default **1 share**) regardless of account size, stop distance,
or signal conviction; the risk core (`autotrader/risk_core.py`) only *validates* that
fixed qty against hard caps and **rejects** anything over a cap (it never resizes).
SELL exits liquidate the full position. The incoming `RoutineSignalPayload` even carries
per-ticker `hard_stops`, but those are **dropped** during normalization and never reach
the sizing decision.

**What we're building (user-confirmed).** Replace the fixed-qty BUY path with
**risk-per-trade sizing off the stop distance**, then **scale by signal confidence**:

```
stop_dist = entry_price - stop_price          # signal hard stop; else entry*(trailing_stop_pct/100)
base_qty  = floor( total_assets * RISK_PER_TRADE_PCT / stop_dist )
qty       = floor( base_qty * confidence_factor )   # factor in [floor, ceil] over [min_conf, 1.0]
```

**Outcome.** A BUY risks a fixed fraction of equity to its stop, sized larger for
higher-conviction signals, and clamped down to cap headroom so a sane size isn't
needlessly rejected. SELL stays full-liquidation. The feature is **default-off**
(`RISK_PER_TRADE_PCT=0.0` → current fixed-qty path), so the existing **128 tests stay
green** and the live `config/risk.config` keeps current behavior until a human opts in.

**Invariants preserved:** sizing is a new **stateless, SDK-free** pure module; signals
still route through `main.py`; `risk_core.evaluate` remains the sole pass/reject gate
(sizer only clamps *down*, never up, never approves); new tunables load from config, none
hardcoded; only `config/risk.config.example` is edited, never the live `config/risk.config`.

## Files to change

### 1. `autotrader/config.py` — three new `RiskConfig` fields (additive, defaulted)
After `trailing_stop_pct` (line 21), add:
```python
risk_per_trade_pct: float = 0.0       # fraction of total_assets risked per BUY; 0 disables (fixed-qty fallback)
confidence_size_floor: float = 0.5    # size factor at confidence == min_confidence
confidence_size_ceil: float = 1.0     # size factor at confidence == 1.0
```
In `load_risk_config()` (line 36) add via the existing `_f` helper:
```python
risk_per_trade_pct=_f("RISK_PER_TRADE_PCT", 0.0),
confidence_size_floor=_f("RISK_CONFIDENCE_SIZE_FLOOR", 0.5),
confidence_size_ceil=_f("RISK_CONFIDENCE_SIZE_CEIL", 1.0),
```
`RISK_PER_TRADE_PCT` is a **fraction** (`0.01` = risk 1% of equity), documented to avoid the `2` vs `0.02` foot-gun. Defaults make `RiskConfig(...)` calls in existing tests valid unchanged.

### 2. `autotrader/domain.py` — optional `stop_price` on `Signal`
Add `stop_price: Optional[float] = None` to the frozen `Signal` dataclass (`Optional`
already imported). In `__post_init__`, if `stop_price is not None` and it's non-finite or
`<= 0`, raise `ValueError` (add `import math`). Do **not** compare stop vs entry here
(no entry price in `Signal`; that's the sizer's job). Default `None` keeps every existing
`Signal(...)` call site and `ThresholdStrategy` output valid.

### 3. `autotrader/signals/normalize.py` — thread per-ticker hard stop into `Signal`
`hard_stops` is keyed by raw ticker. Resolve it with the same `_normalize_symbol` used for
the symbol so casing/qualification match:
```python
def normalize_change(change, confidence_scale=10.0, stop_price=None) -> Signal:
    return Signal(..., stop_price=stop_price)

def normalize_payload(payload, confidence_scale=10.0) -> List[Signal]:
    stops = {_normalize_symbol(k): v for k, v in payload.hard_stops.items()
             if isinstance(v, (int, float)) and math.isfinite(v) and v > 0}  # drop bad stops -> None
    return [normalize_change(c, confidence_scale, stops.get(_normalize_symbol(c.ticker)))
            for c in payload.signal_changes]
```
A bad/non-positive stop drops to `None` (logged) rather than raising, so one bad value
doesn't reject an otherwise-valid signal batch. `SignalInbox.poll` and the webhook inherit
this for free (both go through `normalize_payload`).

### 4. `autotrader/sizing.py` — NEW pure module (the core of the change)
Stateless, no SDK/broker import (mirrors `risk_core.py` / `normalize.py`). Returns a small
frozen `SizeResult(qty: int, reason: str, used_risk_sizing: bool)` so the caller can tell
"0 = too small, don't place" from "fell back to fixed".
```python
def size_position(*, equity, entry_price, signal_stop, confidence, cfg,
                  current_qty, gross_exposure, fixed_qty) -> SizeResult:
    # 0. default-off
    if cfg.risk_per_trade_pct <= 0:
        return SizeResult(fixed_qty, "FIXED_DISABLED", False)
    # 1. stop distance: signal stop (must be 0<stop<entry) -> trailing_stop_pct -> none
    if signal_stop is not None and 0 < signal_stop < entry_price:
        stop_dist = entry_price - signal_stop
    elif cfg.trailing_stop_pct > 0:
        stop_dist = entry_price * (cfg.trailing_stop_pct / 100.0)
    else:
        return SizeResult(fixed_qty, "NO_STOP_DISTANCE_FALLBACK", False)
    if not isfinite(stop_dist) or stop_dist <= 0:
        return SizeResult(fixed_qty, "NO_STOP_DISTANCE_FALLBACK", False)
    # 2. guard inputs
    if equity <= 0 or not isfinite(equity) or entry_price <= 0 or not isfinite(entry_price):
        return SizeResult(0, "SIZED_ZERO_BAD_INPUTS", True)
    # 3. risk base
    base = floor(equity * cfg.risk_per_trade_pct / stop_dist)
    # 4. confidence remap [min_conf,1] -> [floor,ceil], clamped
    t = clamp((confidence - cfg.min_confidence) / max(1e-9, 1.0 - cfg.min_confidence), 0, 1)
    factor = cfg.confidence_size_floor + t * (cfg.confidence_size_ceil - cfg.confidence_size_floor)
    qty = floor(base * factor)
    # 5. clamp DOWN to cap headroom (never up); risk_core stays the final gate
    qty = min(qty, cfg.max_position_qty - current_qty)
    qty = min(qty, floor(cfg.max_order_notional / entry_price))
    qty = min(qty, floor((cfg.max_gross_exposure - gross_exposure) / entry_price))
    if qty <= 0:
        return SizeResult(0, "SIZED_ZERO", True)
    return SizeResult(qty, "SIZED", True)
```
At `confidence == min_confidence` → factor = floor (0.5x); at 1.0 → ceil (1.0x). The
`max(1e-9, ...)` guard handles `min_confidence == 1.0`. BUY-only; SELL never calls this.

### 5. `autotrader/main.py` — integrate at the sizing point (`_route_signal`, lines 95–100)
SELL unchanged. For BUY, call the sizer; early-return on a true zero **before** building
the `OrderRequest` (`OrderRequest.__post_init__` requires `qty > 0`):
```python
pos = next((p for p in snap.positions if p.symbol == signal.symbol), None)
if signal.direction == "SELL" and pos is not None:
    eff_qty = pos.qty
else:
    sr = size_position(equity=snap.total_assets, entry_price=price,
                       signal_stop=signal.stop_price, confidence=signal.confidence,
                       cfg=self._cfg, current_qty=(pos.qty if pos else 0),
                       gross_exposure=snap.gross_exposure(), fixed_qty=self._qty)
    if sr.used_risk_sizing and sr.qty <= 0:
        return TickResult("SIZED_ZERO", f"{signal.symbol}:{sr.reason}")
    if sr.reason == "NO_STOP_DISTANCE_FALLBACK":
        logger.warning("no stop distance for %s; falling back to fixed qty %d", signal.symbol, self._qty)
    eff_qty = sr.qty
```
Add `from autotrader.sizing import size_position`. Downstream (`evaluate`, router, DB,
`_attach_trailing_stop` which already takes `eff_qty`) is unchanged. New `TickResult`
reason code: `SIZED_ZERO`.

### 6. `config/risk.config.example` — document the new knobs (default-off, human-review note)
```bash
# Risk-per-trade sizing (additive; 0 disables -> fixed ORDER_QTY). Fraction of equity:
# 0.01 = risk 1%. qty = floor(equity * RISK_PER_TRADE_PCT / stop_dist) * confidence_factor.
# Risk-limit CHANGES require explicit human review (CLAUDE.md).
RISK_PER_TRADE_PCT=0.0
RISK_CONFIDENCE_SIZE_FLOOR=0.5
RISK_CONFIDENCE_SIZE_CEIL=1.0
```
Do **not** edit the live `config/risk.config`.

## TDD order (tests first, red → green)

1. **`tests/test_sizing.py` (new)** — unit tests for `size_position`: disabled→fixed; basic
   risk size with known numbers (equity 100k, pct 0.01, stop_dist 2 → base 500); confidence
   floor/mid/ceil factors; signal-stop priority over trailing; trailing fallback; no-stop→fixed;
   stop≥entry ignored (no negative dist); zero/non-finite equity & price→`SIZED_ZERO_BAD_INPUTS`;
   fractional floor→0→`SIZED_ZERO`; clamp by position/notional/exposure headroom; clamp never
   raises above base.
2. **`tests/test_domain.py` (modify)** — `Signal` without `stop_price` works; `<=0`/non-finite raises; valid positive accepted.
3. **`tests/test_signal_normalize.py` (modify)** — hard stop threaded to `Signal.stop_price`;
   case-insensitive key match (`"aapl"` ↔ `US.AAPL`); empty stops→`None`; bad stop dropped to `None` (no raise).
4. **`tests/test_main_loop.py` (modify)** — BUY uses risk sizing when enabled (qty ≠ order_qty);
   BUY falls back to fixed when no stop + trailing 0; BUY sizes to 0 → `SIZED_ZERO`, nothing placed;
   SELL still full-liquidation with sizing on; sized qty still passes through risk_core.
5. **`tests/test_config.py` (modify)** — loader defaults the three new env vars correctly; still frozen.

**Keeping 128 green:** all existing `RiskConfig(...)`/`_cfg()` omit the new fields → `risk_per_trade_pct=0.0`
→ fixed path → identical behavior; `Signal.stop_price` defaults `None`.

## Verification

```bash
python -m pytest -q                              # full offline suite: 128 + new, 0 skipped
python -m pytest tests/test_sizing.py -q         # during TDD
python -m pytest tests/test_no_sdk_in_core.py -q # sizing.py must import no SDK
```
Manual REPL sanity (no OpenD):
```python
from autotrader.config import RiskConfig
from autotrader.sizing import size_position
cfg = RiskConfig(trading_env="PAPER", min_confidence=0.6, max_order_notional=1e9,
                 max_position_qty=10000, daily_loss_limit=500, max_gross_exposure=1e9,
                 allowed_symbols=frozenset({"US.AAPL"}), risk_per_trade_pct=0.01)
size_position(equity=100000, entry_price=100, signal_stop=98, confidence=1.0,
              cfg=cfg, current_qty=0, gross_exposure=0, fixed_qty=1)   # -> qty 500
# confidence 0.6 -> factor 0.5 -> qty 250
```

## Edge cases (handled)
- **Floor→0** (tiny pct / wide stop) → `SIZED_ZERO`, never builds a `qty=0` order.
- **Stop ≥ entry** → ignored, falls through; never a negative stop_dist.
- **Zero/non-finite equity or price** → `SIZED_ZERO_BAD_INPUTS`, qty 0.
- **No stop + `trailing_stop_pct=0`** → `NO_STOP_DISTANCE_FALLBACK` → fixed ORDER_QTY + warning (safe, not silent).
- **Stale snapshot** → sizer is pure; `risk_core.evaluate` still rejects on `snapshot.stale` after sizing.
- **Sizer vs risk_core** → sizer clamps DOWN only; `risk_core` remains sole approve/reject authority, never weakened.
- **`RISK_PER_TRADE_PCT` misconfigured high** → notional/position/exposure clamps + risk_core self-limit; fraction semantics documented.
