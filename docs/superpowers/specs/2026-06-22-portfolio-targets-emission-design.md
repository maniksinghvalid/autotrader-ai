# Portfolio-Targets Emission — Design

**Date:** 2026-06-22
**Status:** Approved (design); implementation pending
**Repo of change:** `ai-trading-claude` (producer). **No AutoTrader code change.**
**Related:** [`2026-06-16-portfolio-rebalancing-design.md`](2026-06-16-portfolio-rebalancing-design.md) (the consumer this feeds).

## 1. Problem

AutoTrader's midday `REBALANCE` job (12:30 ET) is fully built and wired but has
**never executed a trade** — for two independent reasons:

1. `RISK_REBALANCE_ENABLED=false` (a human-reviewed risk-config value).
2. The `target_weights` table is **empty** — no `portfolio_targets` have ever
   been ingested.

This spec fixes **#2 only**. The rebalancing feature shipped the entire consumer
half of the chain — `RoutineSignalPayload.portfolio_targets` (schema),
`coerce.py` passthrough (webhook path), `SignalInbox` → `db.upsert_target_weights`
→ `compute_plan` — but **no producer ever populates the field**. The field is
consumed everywhere and produced nowhere.

The data needed already arrives: the daily ticker sweep carries a composite
`new_score` per holding. The producer simply never carries those *levels* into a
`portfolio_targets[]` block (it carries only the BUY/SELL *changes*).

## 2. Where the change lives

The live ingress is the **webhook**, fed by the single-source payload builder
`build_sweep_payload.py`. Confirmed by inspecting processed inbox files: every
recent payload is a `wh-*` (webhook) file; the in-repo file-drop adapter
(`autotrader/signals/routine_adapter.py`) is not currently in the live path.

- **Canonical source (git-tracked):**
  `/Users/acdc/Documents/AI/ai-trading-claude/scripts/build_sweep_payload.py`
- **Runtime copy (what the routine invokes):**
  `~/.claude/skills/trade/scripts/build_sweep_payload.py`
- The two are md5-identical today. The file header warns a copy once "fell
  behind" — the two-copy hazard is handled in §7.

The builder has two modes:
- `sweep` — equity ticker-sweep rows carrying `new_signal` / `new_score` /
  `prior_signal`. **This is the only mode that carries scores → the only mode we
  touch.**
- `options` — overlay postures, no scores. **Left byte-for-byte unchanged.**

Because `coerce.py` already preserves `portfolio_targets` on the webhook path
and the inbox already derives `as_of_date` from the payload timestamp, **no
AutoTrader-side code change is required.** This change purely starts populating a
field everything downstream already expects.

## 3. Key decisions

| # | Decision | Choice |
|---|----------|--------|
| E1 | Producer location | **Skill-side `build_sweep_payload.py`, `sweep` mode**, at the canonical `ai-trading-claude/scripts/` path. The live webhook path; gets real data flowing without changing any ingress contract. |
| E2 | Target set | **All scored rows (full book)** — every swept holding with a composite score becomes a weight target (BUY + HOLD + NEUTRAL). Rebalancing manages the whole book toward score-proportional weights. |
| E3 | Exit handling | **Exits excluded** — `CAUTION`/`AVOID`/`SELL` rows are NOT targets (the DOWN signal-change path already handles exits). |
| E4 | Score floor | **`new_score` must be a finite number `> 0`** to be a target; `≤ 0` / non-finite / missing (quick-tier) rows are excluded. `target_fractions` would drop non-positive scores anyway; excluding at the source is cleaner. |
| E5 | `as_of_date` | **Not carried in the payload** — the receiver (`inbox.py`) derives it from `payload.timestamp.date()`. No schema change. |
| E6 | Symbol form | Reuse the existing `qualify()` → `US.`/`CA.` codes (same allow-list discipline as `signal_changes`). |
| E7 | Activation | **Out of scope.** `RISK_REBALANCE_ENABLED` stays `false`; flipping it is a separate human-reviewed change. This spec delivers the data path only. |

## 4. New component — `build_portfolio_targets(rows)`

A pure function mirroring `build_sweep_changes`, over the same `rows` input. For
each row:

- Read `ticker`, `new_signal`, `new_score`.
- **Include** iff `new_signal` is *not* an exit label (`CAUTION`/`AVOID`/`SELL`)
  **and** `new_score` is a finite number `> 0`.
- Emit `{"symbol": qualify(ticker), "score": float(new_score)}`.
- **Skip** rows with no numeric score (quick-tier; `/trade quick` emits no
  score) and tally them into a non-fatal `warnings` line.
- Dedup by qualified symbol — last row wins.

`signal_changes` and `portfolio_targets` are built from the *same* `rows` with
*independent* filters (BUY/SELL for changes, non-exit-scored for targets). A name
can appear in one, both, or neither.

## 5. Wiring + validation

**Payload assembly** — `build_payload`, `sweep` branch only, gains one key:

```python
payload = {"routine_id": run_id, "timestamp": ts,
           "signal_changes": build_sweep_changes(raw.get("rows", [])),
           "hard_stops": build_hard_stops(raw.get("stops", [])),
           "catalysts": build_catalysts(raw.get("catalysts", [])),
           "portfolio_targets": build_portfolio_targets(raw.get("rows", []))}
```

The `options` branch is untouched (no `portfolio_targets` key).

**Validation** — extend `validate()` to check `portfolio_targets` **when
present**, mirroring the receiver's `TargetWeight` model so a bad payload is
caught at the source rather than silently dropped:
- each item is a dict with exactly `{symbol, score}`,
- `symbol` is a non-empty string,
- `score` is a number (`int`/`float`).

A validation failure writes nothing and exits non-fatally (today's `[warn]
step-W validation failed` behavior).

## 6. Error handling & edge cases

| Case | Behavior |
|---|---|
| No rows / all quick-tier (no scores) | `portfolio_targets: []` → receiver's `if payload.portfolio_targets` is False → no upsert → rebalance returns `NO_TARGETS` (today's safe behavior, unchanged) |
| All rows are exits | `[]` → same as above |
| Duplicate ticker rows | last qualified-symbol wins |
| Score present but `≤ 0` / non-finite | excluded at source |
| Unqualifiable ticker | handled by `qualify()` exactly as `signal_changes` does today |

## 7. Deployment & the two-copy hazard

The runtime invocation is `~/.claude/skills/trade/scripts/build_sweep_payload.py`;
the canonical git-tracked source is `ai-trading-claude/scripts/build_sweep_payload.py`.
They are md5-identical today and must stay so. Plan steps:

1. Edit canonical `scripts/build_sweep_payload.py` + extend its test.
2. Propagate to the runtime copy. **Note:** `sync_claude_dir.sh` mirrors
   `trade/` (not `scripts/`), so the exact propagation path must be confirmed
   during planning — it is an implementation check, not a design decision.
3. **Verify md5 parity** of both copies.
4. Commit in the `ai-trading-claude` repo.

## 8. Testing (TDD)

Extend `ai-trading-claude/scripts/test_build_sweep_payload.py`:

- `test_sweep_emits_portfolio_targets` — rows mixing BUY / STRONG BUY / HOLD /
  NEUTRAL / AVOID / CAUTION + a quick-tier row with no score → assert targets are
  exactly the qualified non-exit scored symbols with correct float scores; exits
  and the score-less row excluded.
- `test_sweep_targets_dedup_and_qualify` — bare ticker qualified to `US.`/`CA.`;
  duplicate ticker → last wins.
- `test_sweep_targets_validation_rejects_bad_score` — malformed target
  (non-numeric score / extra key) → `errs` non-empty, payload `None`.
- `test_options_mode_has_no_portfolio_targets` — options payload key set
  unchanged (guards the options contract).
- `test_empty_when_no_scored_rows` — all exits / all quick-tier →
  `portfolio_targets: []`.

**Cross-repo contract test (AutoTrader-side):** feed a representative
builder-emitted sweep payload through `RoutineSignalPayload.model_validate_json`
+ `coerce_to_canonical` and assert `portfolio_targets` survives as
`TargetWeight`s. This is the one guard against the two repos silently drifting.

## 9. Out of scope

- **No AutoTrader code change** for the data path (consumer already wired).
- **Does not enable rebalancing** — `RISK_REBALANCE_ENABLED` stays `false`.
- **Covered-share reservation on rebalance trims — DEFERRED to a follow-up
  AutoTrader spec.** Known safety gap: the equity-SELL path in
  `risk_core._evaluate` enforces only long-only / notional / exposure; it does
  **not** check that shares being sold are pledged as cover to an open short
  option. A rebalance trim (or any equity SELL) can therefore strip cover from a
  covered call / collar and leave a naked short — and a reduce-only trim also
  skips the daily-loss/notional/exposure caps (the D8 exception). `risk_core`
  already has `_short_option_contracts(snapshot, underlying, right)` to compute
  pledged shares, so the fix is feasible. **This gap must be closed before
  `RISK_REBALANCE_ENABLED` is set true on any book holding option overlays.**
