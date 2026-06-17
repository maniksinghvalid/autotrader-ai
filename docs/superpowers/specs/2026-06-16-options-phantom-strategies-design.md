# Options Phantom Strategies — Design (O2/O3)

**Date:** 2026-06-16
**Branch:** `feat/autotrader-paper-v1`
**Status:** Approved design; pending implementation plan.
**Scope:** Add the four declared-but-unregistered option strategies to the existing
overlay system — **collar**, **bear-put debit spread**, **call diagonal (PMCC)**, and
**long-call LEAP** — under the existing single-audited-path / paper-only safety model.

This builds directly on the O1 overlay foundation
(`docs/superpowers/specs/2026-06-16-options-overlay-trading-design.md`) and composes
with the existing modules: `autotrader/domain.py`, `autotrader/options/{overlays,chain,planner}.py`,
`autotrader/risk_core.py`, `autotrader/config.py`, `autotrader/main.py`.

---

## 1. Background & problem

`OverlayType` already declares `COLLAR`, `CALL_DIAGONAL`, `BEAR_PUT_SPREAD` as enum
placeholders, but they are absent from `overlays.REGISTRY`, so the planner returns
`SKIP_UNSUPPORTED_OVERLAY`. `LEAP` is not in the enum at all. Only `COVERED_CALL` and
`PROTECTIVE_PUT` are live (O1).

Three of the four new strategies have a **short leg whose risk is bounded by another
option leg in the same strategy**, not by underlying shares:

- **Bear-put spread:** short put covered by the long (higher-strike) put — max loss = net debit.
- **Call diagonal / PMCC:** short near-dated call covered by the long deep-ITM LEAP call.
- **Collar:** short call covered by **shares** (already supported); the long put is the floor.
- **LEAP:** a single long leg; defined risk = premium paid.

The current naked-short guard (`risk_core._evaluate_option_leg`, `risk_core.py:129`)
recognizes **share coverage only**, so it would reject the short leg of every spread and
diagonal. Supporting these safely requires the risk core to recognize **intra-strategy
(defined-risk) coverage**.

## 2. Decisions (locked with stakeholder)

1. **Coverage model — defined-risk recognition.** The risk core recognizes that a long
   option leg in the same strategy covers a short leg of the appropriate right/strike/expiry;
   it does not relax to a premium-only budget or to shares-only.
2. **Partial-fill policy — long-first + safe residual.** Always submit the covering long
   leg first and require its non-rejected ack before submitting the short leg. If a later
   leg fails, leave the long-only residual in place (it can never be a naked short) and
   report it loudly. No atomic unwind.
3. **Strategy scope — minimal concrete set.** Exactly the four directional structures below;
   mirror directions (bull-call/bull-put/bear-call verticals, put diagonals, put LEAPs) are
   out of scope and can be added later as registry entries with no schema change.
4. **Selection params — registry-declared; risk caps in config.** Per-leg delta/DTE targets
   are structural and live in each `REGISTRY` entry's `LegSpec`. True risk limits (contract
   cap, premium cap, default contract count, daily-loss, allow-list, enabled overlays) stay
   in `RiskConfig`/env and keep their human-review gate.

## 3. Safety invariants (unchanged, must hold)

- Paper-only (`trading_env == "PAPER"`), enforced before any option leg is evaluated.
- No naked short ever reaches the broker — a short leg is permitted only when shares or a
  covering long leg fully cover it.
- Single audited path: every leg is an `OrderRequest` flowing through
  `risk_core.evaluate()` → `OrderRouter.submit()`. No parallel option channel.
- No `unlock_trade`; OpenD never internet-exposed.
- Risk-limit values live only in `config/` and are human-reviewed.

## 4. Strategy registry (component A)

`domain.py`: add `LEAP = "LEAP"` to `OverlayType`.

`overlays.py`:

- Extend `LegSpec` with three **optional** per-leg overrides, defaulting to `None`:
  - `target_delta: Optional[float] = None`
  - `dte_min: Optional[int] = None`
  - `dte_max: Optional[int] = None`
  When `None`, the planner falls back to `cfg.option_target_delta / option_dte_min /
  option_dte_max`. Existing `COVERED_CALL` / `PROTECTIVE_PUT` legs declare none of these,
  so their behavior is unchanged. `__post_init__` validates: `0 < target_delta <= 1` when
  set; `0 <= dte_min <= dte_max` when both set.
- Add `OverlayDef.single_expiry: bool = False`. When `True`, all legs share the anchor
  leg's chosen expiry (see §5).

Registered entries (anchor leg = `legs[0]`):

| Overlay | `requires_underlying` | `single_expiry` | `legs[0]` | `legs[1]` |
|---|---|---|---|---|
| `COLLAR` | True | True | long PUT, Δ0.30, DTE 30–45 | short CALL, Δ0.30, DTE 30–45 |
| `BEAR_PUT_SPREAD` | False | True | long PUT, Δ0.45, DTE 30–45 | short PUT, Δ0.25, DTE 30–45 |
| `CALL_DIAGONAL` | False | False | long CALL, Δ0.80, DTE 180–365 | short CALL, Δ0.30, DTE 30–45 |
| `LEAP` | False | n/a (1 leg) | long CALL, Δ0.70, DTE 180–365 | — |

Delta/DTE values shown are registry defaults (structural). The anchor leg is always the
**long** (covering) leg, which is also submitted first.

## 5. Contract selection (component B)

`chain.select_contract` gains an optional `pin_expiry: Optional[date] = None`; when set,
candidates are filtered to that exact expiry (in addition to the existing right / premium>0
/ DTE-window / closest-to-|delta| logic, which is otherwise unchanged).

Planner selection flow per overlay:

1. Resolve each leg's effective `(target_delta, dte_min, dte_max)` = `LegSpec` override else config.
2. Select the **anchor leg** (`legs[0]`) first.
3. For `single_expiry=True`: pin every remaining leg to the anchor leg's chosen `expiry`
   (`pin_expiry=anchor.expiry`). For `single_expiry=False` (diagonal): select each leg
   independently within its own DTE window, yielding the near/far split.
4. If any leg has no qualifying contract → `SKIP_NO_CONTRACT`.

## 6. Position sizing (component C)

Two sizing paths in the planner:

- **Share-covered** (`requires_underlying=True`: covered call, collar):
  `contracts = min(held // 100, cfg.max_option_contracts)`; `< 1` → `SKIP_NO_UNDERLYING`.
- **Non-share-covered** (`requires_underlying=False`: bear-put spread, PMCC, LEAP):
  `contracts = min(cfg.option_default_contracts, cfg.max_option_contracts)`; `< 1` →
  `SKIP_OVERLAY_DISABLED` (effectively off). Shares are irrelevant.

All legs of one strategy use the same `contracts` (× `LegSpec.ratio`, default 1), so spread
and diagonal legs are 1:1.

`config.py`: add `option_default_contracts: int = 1` + env `RISK_OPTION_DEFAULT_CONTRACTS`.
It is a position-sizing risk limit (human-reviewed). Default 1 keeps non-covered strategies
small and deterministic.

## 7. Risk-core defined-risk coverage (component D — the heart)

`risk_core.evaluate()` gains an optional `coverage_legs: Tuple[OptionContract, ...] = ()`
(the long legs of the same plan, passed by the engine). Backward compatible — equity and
single-leg O1 calls pass nothing.

Redefine short-OPEN-leg coverage by structure (replacing the shares-only check):

- **Short CALL** covered quantity = `held_shares // 100` (covered call / collar)
  **plus** the contracts of any long CALL — from `coverage_legs` — with
  `strike ≤ short_strike` **and** `expiry ≥ short_expiry` (PMCC). Minus already-open short
  calls on the underlying (`_short_option_contracts`, unchanged) so stacking stays honest.
- **Short PUT** covered quantity = contracts of any long PUT — from `coverage_legs` — with
  `strike ≥ short_strike` **and** `expiry ≥ short_expiry` (bear-put). **Never** shares.
- If covered quantity (in shares) `< need` → reject `uncovered short: ...` (invariant held).

Planner pre-validates structure after selection (defense in depth + clear skip):

- Bear-put: same expiry, long-put strike **>** short-put strike, net debit
  (`long_premium − short_premium`) **> 0**, else `SKIP_INVALID_STRUCTURE`.
- Diagonal: long-call strike **≤** short-call strike and long expiry **>** short expiry,
  else `SKIP_INVALID_STRUCTURE`.

**NLV-derived premium caps (replaces the static `max_option_premium_per_trade` as the
binding limit).** Let `NLV = snapshot.total_assets` and `budget = NLV × option_max_risk_pct`
(new config, default 0.02 = 2%). The premium guard branches by debit vs credit:

- **Debit / BUY OPEN** (long call/put, protective put, long diagonal leg, LEAP): max loss
  is 100% of premium paid, so reject when `qty × premium × multiplier > budget`.
- **Credit / SELL OPEN** (covered call, collar short call, spread/PMCC short leg): under the
  200% stop framework (buy-to-close at 3× entry → loss = 2× premium collected), reject when
  `2 × qty × premium × multiplier > budget` (i.e. collected ≤ `budget / 2`). This runs **in
  addition to** the defined-risk coverage check above. It is an entry-discipline cap; the
  stop that makes "2× premium" the true max loss is enforced in **O4** — documented, not yet
  built (short legs are never naked, so coverage remains the hard safety guarantee meanwhile).

`max_option_premium_per_trade` is retained as an **optional absolute dollar ceiling**
(default `0.0` = off): when `> 0`, the leg's premium dollar amount (paid for debits, collected
for credits) must also be `≤` it, so the effective cap is the tighter of the two.

Other per-leg guards unchanged: allow-list (underlying), contracts cap, daily-loss halt on
OPEN legs.

> **NLV note:** `SimBroker.get_account()` reports `total_assets = cash` only (no position
> market value), so option test fixtures must hold enough cash that NLV supports these caps
> after any seeded share purchase.

**Why intra-plan coverage is sound:** the engine submits long-first and returns early if
the covering leg's ack is `REJECTED`/`UNKNOWN`, so a short leg is only evaluated/submitted
after its cover is acked. SimBroker fills MARKET orders synchronously and v1 is paper-only
(`risk_core.py:53`), so the cover is real by the time the short is placed.

## 8. Engine / partial-fill (component E)

`main._route_overlay` (`main.py:153`) already orders long-before-short and returns on the
first leg failure — that is the safe-residual behavior. Changes:

1. Pass the plan's long-leg `OptionContract`s as `coverage_legs` when calling `evaluate()`
   for a short leg.
2. When a short leg fails (risk-rejected, `REJECTED`, or `UNKNOWN`) **after** a long leg has
   already filled, return a distinct loud `TickResult` — `OVERLAY_RESIDUAL_LONG` with detail
   naming the filled long leg — and `logger.warning` it, so a long-only residual is never
   mistaken for a no-op. (If the long leg itself fails, the existing early return leaves the
   account flat — unchanged.)
3. DB recording per leg is unchanged (each placed leg is recorded); the residual result is
   surfaced in the `TickResult` and logs.

## 9. Config summary (component F)

| Field | Default | Env | Role |
|---|---|---|---|
| `option_default_contracts` | `1` | `RISK_OPTION_DEFAULT_CONTRACTS` | sizing for non-share-covered strategies (NEW) |
| `option_max_risk_pct` | `0.02` | `RISK_OPTION_MAX_RISK_PCT` | fraction of NLV at risk per leg; drives the premium cap (NEW) |
| `max_option_contracts` | `0` | `RISK_MAX_OPTION_CONTRACTS` | per-leg contract cap (existing) |
| `max_option_premium_per_trade` | `0.0` | `RISK_MAX_OPTION_PREMIUM_PER_TRADE` | optional absolute dollar ceiling; `0` = off (existing, repurposed) |
| `allowed_overlays` | `frozenset()` | `RISK_ALLOWED_OVERLAYS` | which overlays enabled (existing) |
| `option_target_delta` / `option_dte_min` / `option_dte_max` | 0.30 / 30 / 45 | existing | **fallback** defaults for legs without overrides |

Operator note (not code): the per-leg premium cap now scales with NLV
(`NLV × option_max_risk_pct`), so a deep-ITM LEAP / PMCC long call is sized against account
equity automatically — no static dollar tuning needed. Set `option_max_risk_pct` to `0.01`
for a 1% ceiling. A vertical's net debit ≤ its long-leg premium, so the debit cap
conservatively bounds spread risk. The **5% buying-power rule** (no single position locks up
> 5% of buying power) is deferred (see §11): it needs a buying-power source and a short-leg
margin model paper v1 doesn't expose cleanly.

## 10. Testing (TDD, mirrors existing test layout)

- `test_options_overlays.py`: 4 new registry entries present with correct legs, per-leg
  Δ/DTE overrides, `single_expiry`, `requires_underlying`; `LegSpec` validation of new fields.
- `test_options_chain.py`: `pin_expiry` filtering; per-leg delta/DTE targeting.
- `test_options_planner.py`: full plan per strategy; collar/spread same-expiry pinning;
  diagonal two-expiry split; LEAP single leg sized by `option_default_contracts`;
  `SKIP_INVALID_STRUCTURE`; `SKIP_NO_CONTRACT` (missing LEAP); sizing fork.
- `test_risk_core_options.py`: short-put-covered-by-long-put approved; short-call-covered-by-
  long-call (PMCC) approved; uncovered short still rejected; collar short call still
  share-covered; net-debit / strike-ordering validation; `coverage_legs` default-empty
  backward compatibility.
- `test_options_e2e.py`: round-trip each of the four on SimBroker (seeded chains incl.
  far-dated LEAP expiries); safe-residual (`OVERLAY_RESIDUAL_LONG`) when the short leg is
  rejected after the long fills; entry-gate blocks OPEN legs; equity path unaffected.
- `test_config*`: `option_default_contracts` parsing/default.

## 11. Out of scope (YAGNI)

- Exit lifecycle / rolls / DTE-to-close enforcement — still **O4**.
- Atomic unwind on partial fill — superseded by the safe-residual decision.
- Full vertical/diagonal families (mirror directions).
- Coverage from **pre-existing** option positions in the account snapshot (v1 coverage is
  intra-plan only; recognizing a previously-opened LEAP as cover for a new short is a
  follow-up requiring option-code parsing of snapshot positions).
- Live async-fill hardening (await the long fill before submitting the short on live OpenD) —
  not needed while v1 is paper-only with synchronous SimBroker fills; noted for the live gate.
- Risk-/confidence-scaled option position sizing — non-covered strategies use a fixed
  `option_default_contracts` for v1.
- **5% buying-power rule** — capping per-position capital locked (premium for debits, margin
  for shorts) at 5% of buying power. Deferred: paper v1 has no clean buying-power / short-margin
  source, and the 2% NLV premium cap is the tighter, more direct per-trade control.
- **Enforcement of the 200% stop-loss** that makes "2× premium collected" the credit leg's
  true max loss — that stop is O4. Until then the credit cap is an entry-discipline filter and
  coverage (no naked shorts) is the hard safety guarantee.

## 12. Files touched

- `autotrader/domain.py` — `LEAP` enum value.
- `autotrader/options/overlays.py` — `LegSpec` overrides, `OverlayDef.single_expiry`, 4 registry entries.
- `autotrader/options/chain.py` — `select_contract(pin_expiry=...)`.
- `autotrader/options/planner.py` — per-leg selection, anchor/pin, sizing fork, structure validation.
- `autotrader/risk_core.py` — `coverage_legs` param + defined-risk coverage logic.
- `autotrader/config.py` — `option_default_contracts`.
- `autotrader/main.py` — `coverage_legs` wiring + `OVERLAY_RESIDUAL_LONG`.
- Tests across the five `tests/test_options_*` / config files.
- Docs: `RUNBOOK.md` (overlay env vars + new strategies), `config/risk.config.example` (if present), this spec.
