# Portfolio-Targets Emission Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Populate the `portfolio_targets[]` block in the sweep-payload builder so AutoTrader's already-wired midday rebalancer finally receives target weights.

**Architecture:** A purely additive change to the producer `build_sweep_payload.py` (`sweep` mode only) in the `ai-trading-claude` repo: a new `build_portfolio_targets(rows)` function, one new key in the sweep payload, and a `validate()` extension. The AutoTrader consumer is untouched except for one cross-repo contract test that locks the receiver against drift.

**Tech Stack:** Python 3, stdlib only in the builder (no pydantic, no project sys.path — it runs in a clean cloud sandbox). The builder's home-grown assert-based test harness (`python3 scripts/test_build_sweep_payload.py`). Pytest on the AutoTrader side.

## Global Constraints

- **Builder is stdlib-only** — no pydantic, no imports from the AutoTrader package. (`build_sweep_payload.py` header.)
- **`options` mode payload stays byte-for-byte unchanged** — never add `portfolio_targets` to it.
- **Target set = full book** — every non-exit row with a finite score `> 0` (BUY / STRONG BUY / HOLD / NEUTRAL). Exit labels `CAUTION` / `AVOID` / `SELL` are never targets.
- **Symbol form** — reuse the existing `qualify()` (`US.`/`CA.` codes); never hand-format symbols.
- **Mirror the receiver** — emitted targets are `{"symbol": <str>, "score": <float>}`, matching `autotrader.signals.schema.TargetWeight`.
- **No AutoTrader behavior change** — `RISK_REBALANCE_ENABLED` stays `false`; this plan delivers the data path only.
- **Two-copy parity** — canonical source is `/Users/acdc/Documents/AI/ai-trading-claude/scripts/build_sweep_payload.py`; runtime copy is `~/.claude/skills/trade/scripts/build_sweep_payload.py`. They must end md5-identical.
- **Existing builder constants to reuse:** `BUY_LABELS = {"BUY","STRONG BUY"}`, `SELL_LABELS = {"CAUTION","AVOID","SELL"}`, `SWEEP_CHANGE_KEYS`, `OPTIONS_CHANGE_KEYS`, `qualify()`, `build_payload(mode, run_id, raw, timestamp=) -> (payload|None, errs, warns)`, `validate(payload, change_keys) -> errs`.

---

### Task 1: `build_portfolio_targets` + wire into sweep payload

**Files:**
- Modify: `/Users/acdc/Documents/AI/ai-trading-claude/scripts/build_sweep_payload.py` (add `import math`; add `build_portfolio_targets`; add one key to the `sweep` branch of `build_payload`, ~lines 229-234)
- Test: `/Users/acdc/Documents/AI/ai-trading-claude/scripts/test_build_sweep_payload.py`

**Interfaces:**
- Consumes: `qualify()`, `SELL_LABELS` (existing module globals).
- Produces: `build_portfolio_targets(rows: List[Dict]) -> Tuple[List[Dict], List[str]]` returning `(targets, skipped_tickers)` where each target is `{"symbol": str, "score": float}`. The sweep payload gains key `"portfolio_targets": List[Dict]`.

> All commands run from `/Users/acdc/Documents/AI/ai-trading-claude/scripts` (the test does `import build_sweep_payload as b`, so the CWD must be `scripts/`).

- [ ] **Step 1: Write the failing tests**

Append to `test_build_sweep_payload.py` (before `def main()`):

```python
def test_sweep_emits_portfolio_targets():
    raw = {"rows": [
        {"ticker": "DIVO", "prior_signal": "HOLD", "new_signal": "BUY", "new_score": 71},
        {"ticker": "O", "prior_signal": "HOLD", "new_signal": "HOLD", "new_score": 55},
        {"ticker": "IAU", "prior_signal": "NEUTRAL", "new_signal": "NEUTRAL", "new_score": 41},
        {"ticker": "YNVDA", "prior_signal": "HOLD", "new_signal": "AVOID", "new_score": 22},   # exit -> excluded
        {"ticker": "SPCE", "prior_signal": "HOLD", "new_signal": "BUY"},                       # no score -> skipped+warned
    ]}
    p, errs, warns = b.build_payload("sweep", "routine-20260622-1455-abc123", raw, timestamp=TS)
    assert errs == [], errs
    tg = {t["symbol"]: t["score"] for t in p["portfolio_targets"]}
    assert tg == {"US.DIVO": 71.0, "US.O": 55.0, "US.IAU": 41.0}      # BUY+HOLD+NEUTRAL; AVOID + no-score excluded
    assert all(isinstance(v, float) for v in tg.values())
    assert any("target(s) skipped" in w for w in warns)               # SPCE reported


def test_sweep_targets_dedup_and_qualify():
    raw = {"rows": [
        {"ticker": "vdy", "new_signal": "BUY", "new_score": 60},      # CA. qualified, lowercased
        {"ticker": "US.AAPL", "new_signal": "HOLD", "new_score": 50},  # already qualified
        {"ticker": "AAPL", "new_signal": "BUY", "new_score": 90},      # dup of US.AAPL -> last wins
    ]}
    p, _, _ = b.build_payload("sweep", "routine-20260622-1455-abc123", raw, timestamp=TS)
    tg = {t["symbol"]: t["score"] for t in p["portfolio_targets"]}
    assert tg == {"CA.VDY": 60.0, "US.AAPL": 90.0}


def test_sweep_empty_when_no_scored_rows():
    raw = {"rows": [
        {"ticker": "YNVDA", "new_signal": "AVOID", "new_score": 22},   # exit
        {"ticker": "SPCE", "new_signal": "BUY"},                       # no score
        {"ticker": "ZAG", "new_signal": "HOLD", "new_score": 0},       # score <= 0
    ]}
    p, errs, _ = b.build_payload("sweep", "routine-20260622-1455-abc123", raw, timestamp=TS)
    assert errs == []
    assert p["portfolio_targets"] == []


def test_options_mode_has_no_portfolio_targets():
    p, errs, _ = _payload("options", {"postures": [
        {"ticker": "AAPL", "position_bias": "LONG", "strategy_outlook": "INCOME", "recommended_strategy": "Covered Call"},
    ]})
    assert errs == []
    assert "portfolio_targets" not in p
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd /Users/acdc/Documents/AI/ai-trading-claude/scripts && python3 test_build_sweep_payload.py`
Expected: FAIL — first failing assertion is in `test_sweep_emits_portfolio_targets` with `KeyError: 'portfolio_targets'` (the sweep payload has no such key yet).

- [ ] **Step 3: Add the `import math` and the builder function**

Add `import math` to the import block (after `import json`). Add this function immediately after `build_sweep_changes` (after ~line 163):

```python
def build_portfolio_targets(rows: List[Dict[str, Any]]) -> Tuple[List[Dict[str, Any]], List[str]]:
    """Map ticker-sweep rows to renormalizable weight targets for the rebalancer.
    Full-book: every non-exit holding carrying a finite, positive composite score
    becomes a target (BUY / STRONG BUY / HOLD / NEUTRAL alike). Exit labels
    (CAUTION / AVOID / SELL) are excluded — the DOWN signal-change path handles
    those. Rows without a usable score (e.g. quick-tier, which emits no score) are
    skipped and reported. Dedup by qualified symbol, last row wins. The receiver
    renormalizes raw scores into weights, so no scaling/clamping happens here.
    Returns (targets, skipped_tickers)."""
    by_symbol: Dict[str, float] = {}
    skipped: List[str] = []
    for r in rows:
        label = str(r.get("new_signal", "")).strip().upper()
        if label in SELL_LABELS:
            continue  # exit — never a target
        try:
            score = float(r.get("new_score"))
        except (TypeError, ValueError):
            skipped.append(str(r.get("ticker")))
            continue
        if not math.isfinite(score) or score <= 0:
            skipped.append(str(r.get("ticker")))
            continue
        by_symbol[qualify(r.get("ticker"))] = score  # last row wins on duplicate
    targets = [{"symbol": s, "score": v} for s, v in by_symbol.items()]
    return targets, skipped
```

- [ ] **Step 4: Wire it into the `sweep` branch of `build_payload`**

Replace the `elif mode == "sweep":` block (~lines 229-234) with:

```python
    elif mode == "sweep":
        targets, t_skipped = build_portfolio_targets(raw.get("rows", []))
        if t_skipped:
            warnings.append("step-W: %d target(s) skipped — no usable score: %s"
                            % (len(t_skipped), ", ".join(t_skipped)))
        payload = {"routine_id": run_id, "timestamp": ts,
                   "signal_changes": build_sweep_changes(raw.get("rows", [])),
                   "hard_stops": build_hard_stops(raw.get("stops", [])),
                   "catalysts": build_catalysts(raw.get("catalysts", [])),
                   "portfolio_targets": targets}
        change_keys = SWEEP_CHANGE_KEYS
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `cd /Users/acdc/Documents/AI/ai-trading-claude/scripts && python3 test_build_sweep_payload.py`
Expected: PASS — final line `All N tests passed.` (N = prior count + 4). The pre-existing `test_sweep_mode_no_overlay_plus_stops_catalysts` and `test_options_regression_2026_06_20` still pass (options payload unchanged; sweep `signal_changes` unchanged).

- [ ] **Step 6: Commit**

```bash
cd /Users/acdc/Documents/AI/ai-trading-claude
git add scripts/build_sweep_payload.py scripts/test_build_sweep_payload.py
git commit -m "feat(routine): emit portfolio_targets from sweep payload builder

Full-book weight targets (non-exit rows with a positive composite score)
for the AutoTrader midday rebalancer, which already consumes the field."
```

---

### Task 2: Validate `portfolio_targets` at the source

**Files:**
- Modify: `/Users/acdc/Documents/AI/ai-trading-claude/scripts/build_sweep_payload.py` (extend `validate()`, before its `return errs`)
- Test: `/Users/acdc/Documents/AI/ai-trading-claude/scripts/test_build_sweep_payload.py`

**Interfaces:**
- Consumes: `validate(payload, change_keys)` (existing), `SWEEP_CHANGE_KEYS` (existing).
- Produces: `validate()` now appends `"target[i] keys" | "target[i] symbol" | "target[i] score"` for malformed `portfolio_targets` entries. No signature change.

- [ ] **Step 1: Write the failing tests**

Append to `test_build_sweep_payload.py` (before `def main()`):

```python
def test_validate_rejects_bad_target_score():
    payload = {"routine_id": "routine-x", "timestamp": TS, "signal_changes": [],
               "hard_stops": {}, "catalysts": [],
               "portfolio_targets": [{"symbol": "US.AAPL", "score": "high"}]}
    errs = b.validate(payload, b.SWEEP_CHANGE_KEYS)
    assert any("target[0] score" in e for e in errs), errs


def test_validate_rejects_bool_target_score():
    payload = {"routine_id": "routine-x", "timestamp": TS, "signal_changes": [],
               "hard_stops": {}, "catalysts": [],
               "portfolio_targets": [{"symbol": "US.AAPL", "score": True}]}  # bool is not a real score
    assert any("target[0] score" in e for e in b.validate(payload, b.SWEEP_CHANGE_KEYS))


def test_validate_rejects_bad_target_keys_and_symbol():
    extra = {"routine_id": "routine-x", "timestamp": TS, "signal_changes": [],
             "hard_stops": {}, "catalysts": [],
             "portfolio_targets": [{"symbol": "US.AAPL", "score": 80.0, "x": 1}]}
    assert any("target[0] keys" in e for e in b.validate(extra, b.SWEEP_CHANGE_KEYS))
    empty_sym = {"routine_id": "routine-x", "timestamp": TS, "signal_changes": [],
                 "hard_stops": {}, "catalysts": [],
                 "portfolio_targets": [{"symbol": "", "score": 80.0}]}
    assert any("target[0] symbol" in e for e in b.validate(empty_sym, b.SWEEP_CHANGE_KEYS))
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd /Users/acdc/Documents/AI/ai-trading-claude/scripts && python3 test_build_sweep_payload.py`
Expected: FAIL — `test_validate_rejects_bad_target_score` fails its assertion (`validate()` currently ignores `portfolio_targets`, so `errs` is empty).

- [ ] **Step 3: Extend `validate()`**

In `validate()`, immediately before `return errs`, add:

```python
    for i, t in enumerate(payload.get("portfolio_targets", [])):
        if not isinstance(t, dict) or set(t) != {"symbol", "score"}:
            errs.append("target[%d] keys" % i)
        elif not (isinstance(t.get("symbol"), str) and t["symbol"]):
            errs.append("target[%d] symbol" % i)
        elif isinstance(t.get("score"), bool) or not isinstance(t.get("score"), (int, float)):
            errs.append("target[%d] score" % i)
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `cd /Users/acdc/Documents/AI/ai-trading-claude/scripts && python3 test_build_sweep_payload.py`
Expected: PASS — `All N tests passed.` (N = Task 1 count + 3). The Task 1 emission tests still pass (builder output is well-formed, so `validate()` finds no target errors there).

- [ ] **Step 5: Commit**

```bash
cd /Users/acdc/Documents/AI/ai-trading-claude
git add scripts/build_sweep_payload.py scripts/test_build_sweep_payload.py
git commit -m "feat(routine): validate portfolio_targets shape, mirroring receiver TargetWeight"
```

---

### Task 3: AutoTrader cross-repo contract test

**Files:**
- Create: `/Users/acdc/Documents/AI/AutoTrader/tests/test_portfolio_targets_contract.py`

**Interfaces:**
- Consumes: `autotrader.signals.coerce.coerce_payload(raw: bytes) -> Optional[bytes]`, `autotrader.signals.schema.RoutineSignalPayload`.
- Produces: nothing — a guard test. Locks that a builder-shaped sweep payload's `portfolio_targets` survives the webhook coerce + Pydantic validation and lands as `TargetWeight`s.

> All commands run from `/Users/acdc/Documents/AI/AutoTrader`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_portfolio_targets_contract.py`:

```python
"""Cross-repo contract: a sweep payload shaped like build_sweep_payload.py's
output (ai-trading-claude) must survive the webhook coerce + schema validation
with its portfolio_targets intact. Guards the two repos against silent drift."""
import json

from autotrader.signals.coerce import coerce_payload
from autotrader.signals.schema import RoutineSignalPayload


def test_builder_sweep_payload_targets_survive_coerce():
    raw = json.dumps({
        "routine_id": "routine-20260622-1455-abc123",
        "timestamp": "2026-06-22T14:55:00",
        "signal_changes": [{"ticker": "US.DIVO", "direction": "UP",
                            "transition": ["HOLD", "BUY"], "points_delta": 7,
                            "driver": "ticker sweep (score 71)"}],
        "hard_stops": {},
        "catalysts": [],
        "portfolio_targets": [{"symbol": "US.DIVO", "score": 71.0},
                              {"symbol": "US.O", "score": 55.0}],
    }).encode()

    out = coerce_payload(raw)
    assert out is not None, "coerce rejected a well-formed builder payload"

    payload = RoutineSignalPayload.model_validate_json(out)
    assert [(t.symbol, t.score) for t in payload.portfolio_targets] == \
           [("US.DIVO", 71.0), ("US.O", 55.0)]


def test_builder_sweep_payload_without_targets_is_empty():
    raw = json.dumps({
        "routine_id": "routine-20260622-1455-abc123",
        "timestamp": "2026-06-22T14:55:00",
        "signal_changes": [],
        "hard_stops": {},
        "catalysts": [],
        "portfolio_targets": [],
    }).encode()

    out = coerce_payload(raw)
    assert out is not None
    payload = RoutineSignalPayload.model_validate_json(out)
    assert payload.portfolio_targets == []
```

- [ ] **Step 2: Run the test to verify it passes immediately (contract already satisfied)**

Run: `cd /Users/acdc/Documents/AI/AutoTrader && python3 -m pytest tests/test_portfolio_targets_contract.py -v`
Expected: PASS — the AutoTrader receiver already supports `portfolio_targets` (this is the consumer that motivated the producer change). This test is a **regression guard**, so it is green on creation. If it FAILS, the receiver contract differs from the spec — stop and reconcile before continuing (do not weaken the test).

- [ ] **Step 3: Commit**

```bash
cd /Users/acdc/Documents/AI/AutoTrader
git add tests/test_portfolio_targets_contract.py
git commit -m "test(signals): lock portfolio_targets producer/consumer contract

Guards that a build_sweep_payload.py-shaped sweep payload survives coerce +
schema validation with targets intact, so the two repos can't drift."
```

---

### Task 4: Deploy to the runtime copy + verify parity

**Files:**
- Sync: `~/.claude/skills/trade/scripts/build_sweep_payload.py` (runtime copy the routine actually invokes — NOT git-tracked)

**Interfaces:**
- Consumes: the committed canonical `ai-trading-claude/scripts/build_sweep_payload.py`.
- Produces: a runtime copy md5-identical to canonical. No commit (the cache copy is outside git).

- [ ] **Step 1: Capture the canonical md5**

Run: `md5 -q /Users/acdc/Documents/AI/ai-trading-claude/scripts/build_sweep_payload.py`
Expected: a 32-char hex hash (call it `CANON`).

- [ ] **Step 2: Propagate to the runtime copy**

First try the repo's sync script (it mirrors `trade/` → `.claude/skills/trade/`; it may or may not cover `scripts/`):

```bash
cd /Users/acdc/Documents/AI/ai-trading-claude && bash scripts/sync_claude_dir.sh
```

Then re-check the runtime copy's hash:

```bash
md5 -q ~/.claude/skills/trade/scripts/build_sweep_payload.py
```

If it does NOT equal `CANON`, the sync script does not cover `scripts/` — fall back to an explicit copy:

```bash
cp /Users/acdc/Documents/AI/ai-trading-claude/scripts/build_sweep_payload.py \
   ~/.claude/skills/trade/scripts/build_sweep_payload.py
```

- [ ] **Step 3: Verify parity (the gate)**

Run:
```bash
diff -q /Users/acdc/Documents/AI/ai-trading-claude/scripts/build_sweep_payload.py \
        ~/.claude/skills/trade/scripts/build_sweep_payload.py && echo "PARITY OK"
```
Expected: `PARITY OK` (no diff output). This closes the "a copy fell behind" hazard called out in the spec (§7) and the file header.

- [ ] **Step 4: Smoke-test the runtime copy end to end**

Run (from the runtime scripts dir so its sibling test imports the runtime module):
```bash
cd ~/.claude/skills/trade/scripts && python3 -c "
import build_sweep_payload as b
raw = {'rows': [{'ticker':'DIVO','new_signal':'BUY','new_score':71},
                {'ticker':'O','new_signal':'HOLD','new_score':55}]}
p, errs, warns = b.build_payload('sweep', 'routine-20260622-1455-abc123', raw)
assert errs == [], errs
print('portfolio_targets:', p['portfolio_targets'])
assert p['portfolio_targets'] == [{'symbol':'US.DIVO','score':71.0},{'symbol':'US.O','score':55.0}], p['portfolio_targets']
print('SMOKE OK')
"
```
Expected: prints the two targets and `SMOKE OK`.

---

## Post-implementation note (not a task)

Rebalancing remains **inert** until `RISK_REBALANCE_ENABLED=true` (a separate, human-reviewed risk-config change) AND the **covered-share reservation gap** (spec §9) is closed by its follow-up AutoTrader plan. Do not flip the flag as part of this work. Once targets start flowing, you can confirm ingestion with:

```bash
sqlite3 ~/.autotrader.db "SELECT as_of_date, COUNT(*), MAX(ingested_at) FROM target_weights GROUP BY as_of_date ORDER BY as_of_date DESC LIMIT 3;"
```
(populated rows = the producer is now feeding the rebalancer; empty still = the routine has not run a sweep since deploy).

## Self-Review

- **Spec coverage:** E1 producer location → Task 1/4 (canonical edit + runtime parity). E2 full-book set + E3 exits excluded + E4 score floor → Task 1 (`build_portfolio_targets` filter) and its tests. E5 as_of_date from timestamp → relied on, exercised by Task 3 contract test. E6 `qualify()` reuse → Task 1. E7 activation out of scope → post-impl note. §5 validation → Task 2. §6 edge cases (empty / dup / ≤0 / all-exit) → Task 1 tests `test_sweep_empty_when_no_scored_rows`, `test_sweep_targets_dedup_and_qualify`. §7 two-copy hazard → Task 4. §8 testing (builder + cross-repo) → Tasks 1-3. §9 out-of-scope deferral → post-impl note. No gaps.
- **Placeholder scan:** none — every code/step is concrete.
- **Type consistency:** `build_portfolio_targets` returns `(List[Dict], List[str])` consistently across Task 1 definition, wiring, and tests; targets are `{"symbol": str, "score": float}` in builder, validator (Task 2), and contract test (Task 3). `build_payload`/`validate` signatures match the existing module.
