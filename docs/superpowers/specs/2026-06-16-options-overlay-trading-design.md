# Options Overlay Trading — Design Spec

**Date:** 2026-06-16
**Branch:** feat/autotrader-paper-v1
**Status:** Approved design (pre-plan)

## Problem

The signal ingress accepts options-flavored webhook payloads, but the engine
flattens them to plain equity trades. Today an options sweep (e.g.
`docs/routinesignal-options.json`) carries overlays like *Covered Call*,
*Protective Put*, *Collar* only as free text in the `driver` field. The pipeline
maps `direction` UP→BUY / DOWN→SELL and places a **market order on the underlying
shares**. The options intent is lost:

- `RoutineSignalPayload` / `SignalChange` have no strike, expiry, right, or
  multi-leg fields.
- `Signal` and `OrderRequest` (`autotrader/domain.py`) are single-symbol equity:
  `symbol + side + qty`. No contract, multiplier, or leg concept.
- The risk core, sizing, and DB all assume one symbol = one equity position.
- A "Protective Put" (DOWN) currently becomes a **SELL of the underlying** —
  arguably the opposite of the intended hedge.

The building blocks already exist on the broker side: the vendored Moomoo skill
ships `get_option_chain`, `get_option_expiration_date`, `resolve_option_code`,
`get_option_volatility`, `get_option_exercise_probability`, `get_option_screen`,
and `place_order.py` accepts option codes (qty in **contracts**, US-option price
to 2 decimals). All missing work is on the AutoTrader side.

## Goal

Place real option contracts end-to-end — covered calls, protective puts, collars,
call diagonals, and bear put spreads — driven by the existing authenticated
webhook → file-drop inbox → local trader pipeline, keeping every safety invariant
intact: **paper-only**, **single audited order path**, **no exposed OpenD
socket**, **no SDK trade-unlock**, **no naked short**.

## Decisions (locked during brainstorming)

| # | Decision | Choice |
|---|----------|--------|
| D1 | Contract selection (strike/expiry/right) | **Local trader selects.** Webhook stays thin; all options logic sits next to the risk core. |
| D2 | Strategy scope | **All five overlays.** Architecture supports all from day one; implementation staged. |
| D3 | Underlying shares missing (<100) | **Skip + report.** Covered call / protective put / collar require ≥100 held shares or are skipped and surfaced. Defined-risk structures (call diagonal, bear put spread) may open standalone. Never write a naked call. |
| D4 | Overlay-type channel | **Explicit schema field.** `SignalChange.overlay` enum covering the full option-strategy range. Absent ⇒ plain equity (today's behavior). Trader never guesses from prose. |
| D5 | Strike/expiry selection rule | **Delta + DTE targets.** Closest-to-target-delta contract within a DTE window. Targets in config, human-reviewed. |
| D6 | Integration shape | **Approach C — overlay expander.** Overlays expand into ordinary leg `OrderRequest`s that flow through the existing router → risk core → broker. |
| D7 | Exit model (v1) | Each overlay closes on whichever fires first: (a) reversing underlying signal, (b) DTE-to-close threshold, (c) profit target. Full roll automation deferred to O4. |

## Architecture (Approach C — overlay expander)

One overlay signal is expanded into 1–N concrete **leg** `OrderRequest`s, which
flow through the **same** `OrderRouter` → `risk_core.evaluate` → broker path that
equities already use. The equity hot path is untouched; options are a front-end
that emits ordinary leg orders. One audit trail, one risk gate, one broker
method.

```
webhook /webhook/sweep  ─┐
file-drop inbox          ─┼─► SignalInbox.poll() ─► normalize ─► Signal(overlay=…)
routine_adapter CLI      ─┘                                          │
                                                                     ▼
                                          options/planner.OverlayPlan (legs | skip)
                                                  uses options/chain (delta+DTE)
                                                  uses options/overlays (registry)
                                                                     │
                                       per leg ▼ (correlation id, ordered)
                                   OrderRouter.submit ─► risk_core.evaluate
                                                       ─► broker.place_order
```

Rejected alternatives: **A — parallel options pipeline** (forks the order path,
two audit surfaces — violates single-audited-path invariant); **B — make
everything option-aware in place** (bloats the equity hot path, tangles two
concerns in `_route_signal` and the risk core).

## Components

### 1. Domain (`autotrader/domain.py`) — additive only

- `OptionRight = Literal["CALL", "PUT"]`.
- `OverlayType` enum — full option-strategy range. Implemented now:
  `COVERED_CALL, PROTECTIVE_PUT, COLLAR, CALL_DIAGONAL, BEAR_PUT_SPREAD`.
  Declared-but-unregistered entries are allowed (planner rejects them with a
  clear "overlay not yet supported" reason).
- `OptionContract` (frozen): `underlying, expiry (date), strike (float),
  right (OptionRight), code (moomoo option code), multiplier (int, default 100)`.
- `OrderRequest` gains:
  - `option: Optional[OptionContract] = None` — `None` ⇒ today's equity order.
  - `position_effect: Literal["OPEN", "CLOSE"] = "OPEN"`.
  - `correlation_id: Optional[str] = None` — groups legs of one overlay.
  - For an option leg, `qty` is **contracts**; `limit_price` is **premium**.
- `__post_init__` validations extended: option leg requires a positive strike,
  a future expiry is not enforced here (planner's job), premium rounded to 2 dp
  for US options at the broker boundary.

### 2. Schema (`autotrader/signals/schema.py`)

- `SignalChange` gains `overlay: Optional[OverlayType] = None`. Absent ⇒ equity.
- 100% backward compatible; existing payloads unchanged.
- `docs/webhook-payload.schema.json` regenerated from the model.
- `normalize.py` carries `overlay` through onto the domain `Signal`
  (`Signal` gains `overlay: Optional[OverlayType] = None`).

### 3. `autotrader/options/` package (new)

- **`chain.py`** — contract selection. Wraps the vendored quote scripts to pick
  the contract **closest to a target delta within a DTE window**. All chain I/O
  confined here; takes a quote/broker handle, returns `OptionContract`s. Pure
  selection logic (given a chain snapshot) is separated from I/O so it is unit
  testable.
- **`overlays.py`** — the **registry**. Each overlay is a pure, stateless
  function: `(underlying position, config) → list of LegSpec (right, side,
  target_delta, dte_window, ratio, position_effect) + an explicit exit rule`.
  Satisfies CLAUDE.md "stateless strategy + explicit exit." Testable against a
  mocked chain, no OpenD.
- **`planner.py`** — `(overlay Signal, AccountSnapshot, chain selector, cfg) →
  OverlayPlan` of concrete leg `OrderRequest`s, **or** a structured skip:
  - `SKIP_NO_UNDERLYING` — covered call / protective put / collar with <100
    shares held.
  - `SKIP_UNSUPPORTED_OVERLAY` — enum value with no registry entry.
  - `SKIP_NO_CONTRACT` — chain has no contract meeting delta/DTE targets.

### 4. Execution (`autotrader/main.py._route_signal`)

- New branch when `signal.overlay` is set:
  1. Build `OverlayPlan`; a skip returns a `TickResult` with the skip reason
     (recorded + reported, never silently dropped).
  2. Submit each leg through the existing `OrderRouter` with a shared
     `correlation_id`.
  3. **Leg ordering:** the risk-reducing / long (debit) leg fills before any
     short leg. On partial failure, **best-effort unwind** of already-filled
     legs + a surfaced alert. **Never leave a naked short.**
- Single-leg overlays are a one-element plan — same code path.
- Confidence filter and entry-window gate apply to overlay entries as today.

### 5. Risk core (`autotrader/risk_core.py`) — option-aware

Mechanism (values are **human-review** `RiskConfig` fields, not set in this
spec):

- Notional for a long (debit) leg = `contracts × premium × multiplier`.
- Short-leg assignment exposure = `contracts × strike × multiplier`.
- New `RiskConfig` fields: `allowed_overlays`, `max_option_contracts`,
  `max_option_premium_per_trade`, `max_option_premium_daily`.
- **Long-only amendment:** a short leg is permitted **only** as part of an
  approved covered/defined overlay plan — covered call backed by ≥100 shares;
  a spread's short leg paired with its long leg in the same correlation group.
  A bare short stays rejected.
- All existing gates (env=PAPER, stale snapshot, daily-loss halt, allow-list,
  reference price, gross exposure) still apply to each leg.

### 6. Broker (`autotrader/moomoo_broker.py`) + `SimBroker`

- `place_order` already accepts option codes; add option-premium pricing (2-dp)
  and route `req.option` → option code, `qty` = contracts.
- Add chain-query methods the selector needs (`get_option_chain`,
  `get_option_expirations`), confined to the adapter.
- Option positions appear in `position_list_query` with option codes —
  reconcile + reporting recognize them.
- `SimBroker` gets a **synthetic deterministic option chain** so the entire
  pipeline tests offline with no OpenD.

### 7. Reporting / DB (`autotrader/db.py`, `autotrader/reporting/`)

- `record_signal` / `record_trade` carry `overlay` + `correlation_id`.
- EOD report groups legs under their overlay (one logical position, N legs).

## Exit model (v1)

Each overlay declares an explicit exit (CLAUDE.md hard rule). v1 closes on
whichever fires first:

- **(a) Reversing underlying signal** — a DOWN/exit signal for the underlying
  closes the overlay.
- **(b) DTE-to-close threshold** — close at ≤ configured days-to-expiry.
- **(c) Profit target** — capture ≥ configured % of premium (short premium
  decayed, or long premium gained).

Full roll automation (rolling strikes/expiries, lifecycle scans) is **O4**.

## Staged implementation

Architecture supports all five overlays from day one; build order keeps each
phase independently testable:

- **O1** — domain + schema + risk + single-leg (covered call, protective put),
  E2E on `SimBroker` → live paper.
- **O2** — multi-leg coordination + collar (atomic-ish leg fill / unwind).
- **O3** — defined-risk spreads (call diagonal, bear put spread; multi-expiry).
- **O4** — exit lifecycle (profit-target / DTE-to-close / reversal-close scans,
  rolls).

## Safety invariants (unchanged, must hold)

- Paper-only (`TrdEnv.SIMULATE`); live still requires `TRADING_ENV=LIVE` + manual
  GUI unlock. No SDK `unlock_trade`.
- Single audited order path: every leg goes through `OrderRouter` (audit-first).
- Webhook stays SDK-free, enqueue-only; OpenD never internet-exposed.
- No naked short — short legs only inside covered/defined overlay plans.
- Risk-limit values changed only under explicit human review.

## Testing strategy

- `overlays.py` and `chain.py` selection logic: pure unit tests against mocked
  chain snapshots, no OpenD.
- `planner.py`: skip-reason coverage (no underlying, unsupported, no contract)
  + concrete leg construction.
- `risk_core`: option notional, short-leg exposure, covered/defined short
  permission, bare-short rejection.
- E2E on `SimBroker` synthetic chain: covered call + protective put open/close,
  multi-leg unwind-on-partial-failure.
- Backward-compat: existing equity payloads still place equity orders unchanged.

## Open items for human review (not decided here)

- Concrete risk-limit values (`max_option_contracts`,
  `max_option_premium_per_trade`, `max_option_premium_daily`, `allowed_overlays`).
- Delta/DTE target values per overlay.
- Profit-target % and DTE-to-close thresholds.
