# LEAP Overlay Properties — Design

**Date:** 2026-06-18
**Branch:** `feat/autotrader-paper-v1`
**Status:** Approved (design); pending implementation plan
**Scope:** Standalone `LEAP` overlay only. `CALL_DIAGONAL` (PMCC) selection params are **not** changed.

## Problem

The question raised: should LEAPs use the same contract-selection properties as the
short-dated overlays —

```
RISK_OPTION_TARGET_DELTA=0.30
RISK_OPTION_DTE_MIN=45
RISK_OPTION_DTE_MAX=90
```

— or different ones?

**Answer: different, on three axes.** A LEAP is a long-term, deep-ITM
stock-replacement position; the `0.30 delta / 45–90 DTE` profile is a short-dated
OTM hedge/income profile. Applying the latter to a LEAP is a category error (a
cheap speculative call, not a stock substitute).

The LEAP entry already overrides the global config
([`overlays.py`](../../../autotrader/options/overlays.py)) to `delta=0.70,
dte_min=180, dte_max=365`, but those values are themselves only half-right:

| Property | Current | Best practice (PMCC/LEAP literature) | Issue |
|---|---|---|---|
| Delta | 0.70 | 0.70–0.80, deep ITM, ~stock-like | low end |
| DTE **buy** window | 180–365 | 12+ months | buys a 6-month "fake LEAP"; never buys a true 12mo+ |
| Roll / exit | global `dte_to_close=7` | roll at ~6 months remaining | a LEAP held to 7 DTE = catastrophic theta loss |

**Critical interaction:** the option-chain fetch (`MoomooBroker.get_option_chain`,
commit `c913ef7`) only reaches `today..+90 days`. LEAPs (180–730 DTE) and the
existing PMCC long leg (180–365 DTE) are therefore **unreachable** — they would
`SKIP_NO_CONTRACT` regardless of selection params. The LEAP-eligible contracts do
exist on Moomoo paper (verified 2026-06-18: AAPL expiries out to 2028-12, SPCE to
2028-01); the fetch horizon is the blocker.

## Decisions

| Property | Value | Rationale |
|---|---|---|
| LEAP delta | **0.80** | More stock-like, less theta risk; matches PMCC long leg |
| LEAP DTE window | **180–730** (pragmatic wide) | 6mo floor, 2yr cap; cap avoids thin far-dated spreads; wide enough to trade sparse names |
| Within-window selection | **prefer longest DTE** | Wide window + nearest-expiry would collapse liquid names to ~7mo calls; longest yields true LEAPs (AAPL → ~21mo) and best-available on sparse names (SPCE → ~7mo) |
| Roll / close DTE | **120** | Below the 180 buy floor so even a floor-bought LEAP holds ~3 months; 0.80Δ deep-ITM theta stays mild until ~90 DTE |
| Profit target | **none (ride it)** | Long stock-replacement; don't cap a long-term bullish thesis. Exit discipline = roll at 120 DTE + underlying stop |
| Scope | **standalone LEAP only** | PMCC (`CALL_DIAGONAL`) selection params unchanged |
| Params location | **REGISTRY hardcode** | Matches existing overlays (COLLAR / BEAR_PUT / CALL_DIAGONAL hardcode their structural params); no new config keys |

## Components

### 1. `overlays.py` — LEAP REGISTRY entry
```python
OverlayType.LEAP: OverlayDef(
    requires_underlying=False,
    single_expiry=False,
    dte_to_close=120,            # per-overlay exit override (new)
    profit_target_pct=None,      # ride it (new; None = no target)
    legs=(
        LegSpec(right="CALL", side="BUY",
                target_delta=0.80, dte_min=180, dte_max=730,
                prefer_longest=True),   # new flag
    ),
)
```

### 2. `LegSpec.prefer_longest: bool = False`
Marks a long-dated leg. Only the LEAP leg sets it `True`. All other legs (incl.
the PMCC long leg) keep the default `False` (nearest-expiry), preserving behavior.

### 3. `chain.py` — `select_contract(..., prefer_longest=False)`
When `True`, the tie-break sorts by **longest** DTE instead of nearest (negate the
days term); delta-closeness remains the primary sort key. `premium > 0` and the
DTE-window filter are unchanged. Default `False` preserves every existing
overlay's selection exactly.

Tie-break key:
- default: `(abs(abs(delta) - target), days_to_expiry, strike)`
- prefer_longest: `(abs(abs(delta) - target), -days_to_expiry, strike)`

### 4. Per-overlay exit declaration
`OverlayDef` gains `dte_to_close` and `profit_target_pct` overrides. **Both must
distinguish "not overridden" (fall back to `cfg`) from "overridden to disabled"
(`None`)** — LEAP sets `profit_target_pct` to disabled, and a naive `override or
cfg` would resurrect the global `0.5` because `None or 0.5 == 0.5`. Use a module
sentinel:

```python
_UNSET = object()
# OverlayDef fields default to _UNSET
dte_to_close = ov.dte_to_close if ov.dte_to_close is not _UNSET else cfg.option_dte_to_close
profit_target_pct = (ov.profit_target_pct if ov.profit_target_pct is not _UNSET
                     else cfg.option_profit_target_pct)
```

`ExitRule.profit_target_pct` becomes `Optional[float]` where `None` means "no
profit target." LEAP sets `dte_to_close=120` and `profit_target_pct=None`.

This is **declaration only** — there is no O4 close-scan / roll engine yet
(`overlays.py` docstring: "ENFORCEMENT … is O4"). This change records correct
per-overlay intent; enforcement is out of scope.

### 5. Fetch path — required infrastructure
Touches `broker.py` (ABC), `moomoo_broker.py`, `sim_broker.py`, and the planner
call site. Replaces the blind 30-day-chunk-over-90-days fetch from `c913ef7`.

- **Signature:** `get_option_chain(underlying, right, dte_min, dte_max)`. The
  planner already computes each leg's `dte_min`/`dte_max`
  ([`planner.py`](../../../autotrader/options/planner.py)) and passes them through.
- **MoomooBroker:** enumerate expiries once via `get_option_expiration_date`
  (no 30-day span limit — returns the full multi-year list in one call), filter to
  `[today+dte_min, today+dte_max]`, fetch the chain only for qualifying expiries
  (grouped into ≤30-day buckets to respect the span cap), then `get_market_snapshot`
  in ≤400-code batches (per `API_LIMITS.md`).
- **SimBroker:** accept the two params; continue returning its canned chain
  (the precise DTE filtering still happens in `select_contract`).
- **Effect:** a protective put fetches ~2 expiries instead of 25 blind windows; a
  LEAP fetches its ~8 in-band expiries; the PMCC long leg becomes reachable again
  with **no change to its selection params**.

## Data Flow

```
runner → planner.build_overlay_plan
  per leg:
    dte_min, dte_max, target_delta, prefer_longest ← LegSpec (or cfg fallback)
    quotes ← chain_provider.get_option_chain(underlying, right, dte_min, dte_max)
               └─ MoomooBroker: get_option_expiration_date → filter to band
                  → get_option_chain per ≤30d bucket → get_market_snapshot (≤400)
    q ← select_contract(quotes, right, target_delta, dte_min, dte_max, asof,
                        prefer_longest=prefer_longest)
    q is None → OverlaySkip(SKIP_NO_CONTRACT)
  exit ← ExitRule(dte_to_close   = overlay.dte_to_close   if set else cfg.option_dte_to_close,
                  profit_target_pct = overlay.profit_target_pct if set else cfg.option_profit_target_pct)
                  # "if set" = "is not _UNSET" sentinel; None is a valid override (disabled)
```

## Error Handling
- Chain fetch per bucket: a bad/empty sub-window logs and is skipped, never aborts
  the others (preserve the `c913ef7` resilience; error payload is a `str` when
  `ret != RET_OK`).
- `select_contract` returns `None` → `SKIP_NO_CONTRACT` (unchanged contract).
- All existing `ret_code == RET_OK` checks retained.

## Testing
- **Unit (`chain.py`):** `prefer_longest=True` picks the longest in-window contract
  at the target delta; `prefer_longest=False` unchanged (nearest).
- **Unit (planner):** LEAP plan resolves an 0.80Δ contract at the longest in-window
  expiry; LEAP `ExitRule` = `dte_to_close=120, profit_target_pct=None`;
  short overlays' exit still falls back to `cfg`.
- **Unit (overlays):** `LegSpec.prefer_longest` default `False`; `OverlayDef`
  exit overrides default `None`.
- **Unit (sim broker):** new `get_option_chain` signature; existing planner tests
  pass with windowed call.
- **Live smoke (optional, OpenD):** LEAP resolves for a liquid name (AAPL) and a
  sparse name (SPCE) per the 2026-06-18 manual verification.

## Out of Scope
- O4 exit/roll **enforcement** (close-scan engine).
- PMCC (`CALL_DIAGONAL`) selection-param changes (reachability is fixed for free by
  the fetch path; params stay 180–365, nearest-expiry, 0.80Δ long / 0.30Δ short).
- New `RISK_OPTION_LEAP_*` config keys (chose REGISTRY hardcode to match the repo
  pattern; can be revisited).
```
