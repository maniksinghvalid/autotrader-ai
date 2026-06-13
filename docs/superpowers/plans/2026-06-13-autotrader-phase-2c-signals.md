# AutoTrader Phase 2c — Signals (Pydantic External-Signal Ingress · Broker-Resting Trailing Stops)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add the two remaining Phase-2 capabilities on top of the 2b continuous loop: a **localhost-only file-drop ingress** that validates external signals (Gemini's `RoutineSignalPayload` Pydantic schema) and normalizes them into `domain.Signal` values routed through the *same* risk core, and **broker-resting `TRAILING_STOP` stops** (5%) attached to every BUY entry so an exit survives an OpenD outage (research R5) — all offline-testable with `SimBroker` and fakes, no network, no real sleeping, no OpenD.

**Architecture:** Two cohesive additions sit on top of the unchanged deterministic core (strategy → `risk_core.evaluate()` → `OrderRouter`). **Ingress:** a new `autotrader/signals/` package — `schema.py` (Pydantic models), `normalize.py` (payload → `domain.Signal`), `inbox.py` (`SignalInbox` watches a directory, validates each `*.json`, moves it to `processed/` or `rejected/`). External signals enter `TradeEngine` via a new `submit_external_signal()`, which shares one extracted `_route_signal()` with `tick()` so there is exactly one validated order path (research C4 — neither engine bypasses the risk core). **Trailing stops:** `OrderRequest` gains a first-class `TRAILING_STOP` order type; after a BUY entry places, the engine attaches a broker-resting `TRAILING_STOP` SELL through the audited router + risk core. None of the new modules import the SDK; the signals layer is added to the SDK-confinement guard.

**Tech Stack:** Python 3.11 · `pydantic >= 2.7` (validation) · sqlite3/threading (stdlib) · `zoneinfo` (stdlib, from 2b) · pytest ≥ 8.0

---

## Phase 2 Scope Note

This is the third and final Phase-2 execution plan (see the 2a/2b scope notes and roadmap §4):
- **Plan 2a — Foundation (DONE):** carry-over fixes + rate limiter + SQLite WAL projection.
- **Plan 2b — Engines (DONE):** continuous loop + EST lifecycle scheduler + watchdog + ground-truth sync + entry gate.
- **Plan 2c — Signals (this plan):** Pydantic external-signal ingress (localhost-only) + broker-resting `TRAILING_STOP` stops.

**Two subsystems, one plan:** ingress and trailing-stops are bundled by the roadmap as "2c Signals" and share the engine/runner wiring (external signals route through the same path that also attaches stops). They are kept in one plan but each task produces independently working, tested software.

**Ingress transport decision — file-drop, not a bound HTTP port.** The roadmap allows "file-drop *or* a 127.0.0.1-bound endpoint". A watched directory has **no network surface at all**, is the strongest possible reading of CLAUDE.md's "no internet-reachable order path", and is trivially offline-deterministic (no FastAPI/uvicorn, no socket, no async). 2c uses the file-drop.

> **2c-W addendum (webhook ingress).** A later increment adds an authenticated, enqueue-only HTTP **producer** (`autotrader/signals/webhook.py`, behind ngrok) that writes validated `RoutineSignalPayload`s into this same inbox. The file-drop is **retained as the durable hand-off boundary** between the internet-facing receiver (no broker handle, no SDK) and the broker-connected trader (which still consumes via `SignalInbox.poll()` → risk core). They compose; the order path stays local. See `RUNBOOK.md` §12b and the CLAUDE.md "no internet order path" rule for the H1-mitigation decision record.

**Deliberately deferred to Phase 3 / later:** stop-consolidation protocol (cancel-old-before-new when a position's qty changes — Phase 3 §4.3), catalyst-driven logic, `hard_stops` execution (validated and carried by the payload but not acted on), dashboard read-wiring, ex-dividend logic (Phase 4). 2c attaches **one** trailing stop per entry (flat→long); refreshing/consolidating stops on pyramiding is Phase 3.

---

## Acceptance Criteria

The plan is **done** only when every behavioral criterion below holds AND its verification command is green. Each behavioral criterion (BC) states an observable guarantee; the verification commands (VC) mechanically prove it. The live "multi-week clean paper session" exit gate is human-verified and stated separately at the end (out of automated scope for 2c).

### Behavioral acceptance criteria (observable guarantees)

**BC-1 — External payloads are validated and normalized to `domain.Signal`.**
Given a `RoutineSignalPayload`, when normalized, then `UP`→`BUY` / `DOWN`→`SELL`, a bare ticker `AAPL`→`US.AAPL` (qualified symbols pass through, upper-cased), and confidence is derived from `|points_delta|` scaled and clamped to `[0,1]`. An invalid `direction` is rejected by the schema.
*Proven by:* VC-1.

**BC-2 — A malformed signal file never crashes the loop; valid files are consumed exactly once.**
Given files dropped in the inbox, when `poll()` runs, then a valid `*.json` yields normalized signals and moves to `processed/`, a malformed file moves to `rejected/` and yields nothing, and a second `poll()` re-processes nothing.
*Proven by:* VC-2.

**BC-3 — External signals route through the SAME validated path; they never bypass the risk core.**
Given an external `Signal`, when `submit_external_signal()` runs, then it passes the confidence filter, entry gate, risk core, and router exactly as a strategy signal does (research C4). A BUY places; a low-confidence signal is dropped.
*Proven by:* VC-6 (`test_submit_external_signal_*`), VC-7 (`test_run_once_routes_external_signal_from_inbox`).

**BC-4 — External signals are gated by the watchdog and the entry window.**
Given an unhealthy watchdog, when an iteration runs, then the inbox is **not** polled and no external order places; given a closed entry gate, an external BUY reports `ENTRY_CLOSED`.
*Proven by:* VC-6 (`test_submit_external_signal_blocked_by_entry_gate`), VC-7 (`test_run_once_skips_inbox_when_unhealthy`).

**BC-5 — A BUY entry attaches a broker-resting trailing stop when enabled, and none when disabled.**
Given `trailing_stop_pct = 5.0`, when a BUY entry places, then a resting `TRAILING_STOP` SELL for the entry qty exists at the broker; given `trailing_stop_pct = 0.0`, no resting stop is attached.
*Proven by:* VC-6 (`test_buy_entry_attaches_trailing_stop`, `test_no_trailing_stop_when_disabled`).

**BC-6 — `TRAILING_STOP` is a first-class, validated order that rests and is flattenable.**
Given a `TRAILING_STOP` `OrderRequest`, when constructed without `trail_percent` (or with a `limit_price`), then it raises; when placed on `SimBroker`, it rests (does not fill, no position change) and is cleared by `cancel_all()`.
*Proven by:* VC-3, VC-4.

**BC-7 — Trailing stops route through the risk core + audited router; no new order path.**
Given the trailing-stop attach, when it runs, then the order flows through `risk_core.evaluate()` → `OrderRouter.submit()` (audit line written first), the same path every other order uses.
*Proven by:* VC-6 (the stop appears only after a router-placed, risk-approved SELL), plus the unchanged router/risk core.

**BC-8 — The trailing-stop percent is config-driven, defaults disabled, and is immutable at runtime.**
Given no env, when config loads, then `trailing_stop_pct == 0.0` (disabled); given `RISK_TRAILING_STOP_PCT=5.0`, it loads `5.0`. The frozen `RiskConfig` cannot be mutated.
*Proven by:* VC-5.

**BC-9 — The signals layer never imports the SDK.**
Given `signals.schema`, `signals.normalize`, `signals.inbox` imported in a fresh process, then `moomoo` is **not** loaded.
*Proven by:* VC-8.

**BC-10 — No regression in the existing core.**
Given the full offline suite, when it runs, then every prior test (Phase 1 + 2a + 2b) still passes and nothing is skipped.
*Proven by:* VC-9.

### Verification commands (mechanical proof — all must be green)

| VC | Command | Expected | Proves |
|---|---|---|---|
| VC-1 normalize | `pytest tests/test_signal_normalize.py` | 5 passed | BC-1 |
| VC-2 inbox | `pytest tests/test_signal_inbox.py` | 4 passed | BC-2 |
| VC-3 domain | `pytest tests/test_domain.py` | 8 passed | BC-6 |
| VC-4 sim broker | `pytest tests/test_sim_broker.py` | 7 passed | BC-6 |
| VC-5 config | `pytest tests/test_config.py` | 4 passed | BC-8 |
| VC-6 engine | `pytest tests/test_main_loop.py` | 16 passed | BC-3, BC-5, BC-7 |
| VC-7 runner | `pytest tests/test_runner.py` | 8 passed | BC-3, BC-4 |
| VC-8 SDK confinement | `pytest tests/test_no_sdk_in_core.py` | 1 passed (now also imports `signals.schema`, `signals.normalize`, `signals.inbox`) | BC-9 |
| VC-9 full suite | `pytest tests/ --ignore=tests/test_moomoo_broker_live.py` | 118 passed, 0 skipped | BC-10 |

> Count math for VC-9: 97 (after 2b) + 5 normalize + 4 inbox + 2 domain + 2 sim broker + 1 config + 5 engine + 2 runner = **118**. If your post-2b baseline differs, adjust to baseline + 21 and keep "0 skipped".

### Live exit gate (human-verified — NOT part of automated 2c completion)

Per roadmap §4 Phase 2, before promoting past Phase 2 the system must additionally demonstrate, against a real OpenD paper account: **N clean unattended sessions** (full 08:30→16:15 lifecycle, no unhandled exceptions), **P&L and max-drawdown tracked** within configured limits, and **zero safety violations** — and now additionally: **every BUY entry carries a live broker-resting trailing stop** (verify in the OpenD GUI), and **external signals only ever enter via the local inbox** (no listening socket). This gate is signed off by a human reviewer and recorded in the roadmap; it is not asserted by the offline suite.

---

## File Structure

| File | Action | Responsibility |
|---|---|---|
| `pyproject.toml` | **Modify** | Add `pydantic>=2.7` dependency |
| `autotrader/signals/__init__.py` | **Create** | Package marker for the ingress layer |
| `autotrader/signals/schema.py` | **Create** | Pydantic `SignalChange` / `Catalyst` / `RoutineSignalPayload` |
| `autotrader/signals/normalize.py` | **Create** | `normalize_payload()` / `normalize_change()` → `domain.Signal` |
| `autotrader/signals/inbox.py` | **Create** | `SignalInbox.poll()` — file-drop ingress, validate + move |
| `autotrader/domain.py` | **Modify** | `OrderType` += `TRAILING_STOP`; `OrderRequest.trail_percent` + validation |
| `autotrader/sim_broker.py` | **Modify** | `TRAILING_STOP` orders rest (never auto-fill) |
| `autotrader/moomoo_broker.py` | **Modify** | `place_order` `TRAILING_STOP` branch (`TrailType.RATIO`, live-only path) |
| `autotrader/router.py` | **Modify** | Add `trail_percent` to the intent audit line |
| `autotrader/config.py` | **Modify** | `RiskConfig.trailing_stop_pct` (default `0.0`) + loader |
| `autotrader/main.py` | **Modify** | Extract `_route_signal()`; add `submit_external_signal()` + `_attach_trailing_stop()`; wire `SignalInbox` into `main()` |
| `autotrader/runner.py` | **Modify** | Optional `signal_inbox`; poll + route external signals when healthy |
| `config/risk.config.example` | **Modify** | Document `RISK_TRAILING_STOP_PCT` + `AUTOTRADER_SIGNAL_INBOX` |
| `tests/test_signal_normalize.py` | **Create** | 5 tests |
| `tests/test_signal_inbox.py` | **Create** | 4 tests |
| `tests/test_domain.py` | **Modify** | +2 trailing-stop validation tests |
| `tests/test_sim_broker.py` | **Modify** | +2 resting-stop tests |
| `tests/test_config.py` | **Modify** | +1 trailing-stop-pct loader test |
| `tests/test_main_loop.py` | **Modify** | +5 external-signal + trailing-stop tests |
| `tests/test_runner.py` | **Modify** | +2 inbox-routing tests |
| `tests/test_no_sdk_in_core.py` | **Modify** | Add the 3 new `signals.*` modules to the import list |

---

### Task 1: `pydantic` dependency + signal schema + normalizer

**Files:**
- Modify: `pyproject.toml`
- Create: `autotrader/signals/__init__.py`
- Create: `autotrader/signals/schema.py`
- Create: `autotrader/signals/normalize.py`
- Create: `tests/test_signal_normalize.py`

- [ ] **Step 1: Add `pydantic` to dependencies and install it**

In `pyproject.toml`, change the `dependencies` line:

```toml
dependencies = ["moomoo-api>=10.4.6408", "tzdata>=2024.1", "pydantic>=2.7"]
```

Install it now so the tests (and the subprocess confinement test in Task 7) can import it:

```bash
cd /Users/acdc/Documents/AI/AutoTrader
python3 -m pip install "pydantic>=2.7"
```

- [ ] **Step 2: Create the package marker**

Create `autotrader/signals/__init__.py`:

```python
"""External-signal ingress layer. Pydantic validation + normalization to neutral
domain.Signal values. Imports pydantic + autotrader.domain only — never the SDK."""
```

- [ ] **Step 3: Write the failing tests**

Create `tests/test_signal_normalize.py`:

```python
"""normalize_payload turns a validated RoutineSignalPayload into domain.Signal
values: UP->BUY / DOWN->SELL, ticker->US.TICKER, points_delta->confidence."""
import pytest
from pydantic import ValidationError

from autotrader.signals.schema import RoutineSignalPayload, SignalChange
from autotrader.signals.normalize import normalize_change, normalize_payload


def _change(**over):
    base = dict(ticker="AAPL", direction="UP", transition=["50", "200"],
                points_delta=10, driver="breakout")
    base.update(over)
    return SignalChange(**base)


def test_payload_normalizes_each_change_to_signal():
    payload = RoutineSignalPayload(
        routine_id="r1", timestamp="2026-06-12T09:46:00-04:00",
        signal_changes=[_change(ticker="AAPL", direction="UP"),
                        _change(ticker="MSFT", direction="DOWN")])
    sigs = normalize_payload(payload)
    assert [s.symbol for s in sigs] == ["US.AAPL", "US.MSFT"]
    assert [s.direction for s in sigs] == ["BUY", "SELL"]


def test_qualified_symbol_passes_through_uppercased():
    sig = normalize_change(_change(ticker="us.nio"))
    assert sig.symbol == "US.NIO"


def test_confidence_scales_and_clamps_to_one():
    assert normalize_change(_change(points_delta=5)).confidence == pytest.approx(0.5)
    assert normalize_change(_change(points_delta=50)).confidence == 1.0   # clamped
    assert normalize_change(_change(points_delta=0)).confidence == 0.0    # drops at filter


def test_custom_confidence_scale_overrides_default():
    sig = normalize_change(_change(points_delta=5), confidence_scale=5.0)
    assert sig.confidence == 1.0


def test_invalid_direction_is_rejected_by_schema():
    with pytest.raises(ValidationError):
        SignalChange(ticker="AAPL", direction="SIDEWAYS", transition=[],
                     points_delta=1, driver="x")
```

- [ ] **Step 4: Run tests to verify they fail**

```bash
python3 -m pytest tests/test_signal_normalize.py -v
```

Expected: FAIL with `ModuleNotFoundError: No module named 'autotrader.signals.schema'`

- [ ] **Step 5: Create `autotrader/signals/schema.py`**

```python
"""Pydantic v2 schema for the external-signal ingress — Gemini's
RoutineSignalPayload, adopted from the bot design (roadmap §4: "the schema is
good"). Validated payloads are normalized to domain.Signal by normalize.py
BEFORE they ever reach the confidence filter + risk core. Imports pydantic
only — never the moomoo SDK."""
from __future__ import annotations

from datetime import datetime
from typing import Dict, List, Literal

from pydantic import BaseModel, Field


class SignalChange(BaseModel):
    ticker: str
    direction: Literal["UP", "DOWN"]
    transition: List[str] = Field(default_factory=list)
    points_delta: int
    driver: str = ""


class Catalyst(BaseModel):
    ticker: str
    event: str
    date: str
    value: float


class RoutineSignalPayload(BaseModel):
    routine_id: str
    timestamp: datetime
    signal_changes: List[SignalChange]
    # Validated and carried, but NOT executed in 2c (hard-stop execution and
    # catalyst logic are later phases — YAGNI).
    hard_stops: Dict[str, float] = Field(default_factory=dict)
    catalysts: List[Catalyst] = Field(default_factory=list)
```

- [ ] **Step 6: Create `autotrader/signals/normalize.py`**

```python
"""Normalize a validated RoutineSignalPayload into neutral domain.Signal values.

Mapping (deterministic, no I/O):
  direction:  UP -> BUY, DOWN -> SELL
  symbol:     bare 'AAPL' -> 'US.AAPL'; an already-qualified 'US.AAPL' passes
              through; always upper-cased
  confidence: |points_delta| scaled by confidence_scale and clamped to [0, 1]
              (points_delta >= scale -> 1.0; points_delta 0 -> 0.0, which the
              confidence filter drops as an explicit no-trade).

confidence_scale is a signal-shaping parameter (NOT a risk limit), so it is a
function argument with a default — not a RiskConfig field."""
from __future__ import annotations

from typing import List

from autotrader.domain import Signal
from autotrader.signals.schema import RoutineSignalPayload, SignalChange

_DIRECTION = {"UP": "BUY", "DOWN": "SELL"}


def _normalize_symbol(ticker: str) -> str:
    t = ticker.strip().upper()
    return t if "." in t else f"US.{t}"


def _confidence(points_delta: int, scale: float) -> float:
    if scale <= 0:
        return 0.0
    return max(0.0, min(1.0, abs(points_delta) / scale))


def normalize_change(change: SignalChange, confidence_scale: float = 10.0) -> Signal:
    return Signal(
        symbol=_normalize_symbol(change.ticker),
        direction=_DIRECTION[change.direction],
        confidence=_confidence(change.points_delta, confidence_scale),
        rationale=f"{change.driver or 'external'}: "
                  f"{'/'.join(change.transition) or change.direction}",
    )


def normalize_payload(payload: RoutineSignalPayload,
                      confidence_scale: float = 10.0) -> List[Signal]:
    return [normalize_change(c, confidence_scale) for c in payload.signal_changes]
```

- [ ] **Step 7: Run tests to verify they pass**

```bash
python3 -m pytest tests/test_signal_normalize.py -v
```

Expected: 5 passed.

- [ ] **Step 8: Commit**

```bash
git add pyproject.toml autotrader/signals/__init__.py autotrader/signals/schema.py autotrader/signals/normalize.py tests/test_signal_normalize.py
git commit -m "feat: Pydantic external-signal schema + normalizer (-> domain.Signal)"
```

---

### Task 2: File-drop `SignalInbox`

**Files:**
- Create: `autotrader/signals/inbox.py`
- Create: `tests/test_signal_inbox.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_signal_inbox.py`:

```python
"""SignalInbox: file-drop ingress. poll() validates+normalizes *.json payloads,
moving each to processed/ (ok) or rejected/ (bad). A malformed file never crashes
the loop, and a consumed file is never re-processed."""
import json

from autotrader.signals.inbox import SignalInbox

_PAYLOAD = {
    "routine_id": "r1", "timestamp": "2026-06-12T09:46:00-04:00",
    "signal_changes": [
        {"ticker": "AAPL", "direction": "UP", "transition": ["50", "200"],
         "points_delta": 10, "driver": "breakout"}
    ],
}


def test_poll_reads_valid_file_and_moves_to_processed(tmp_path):
    inbox = SignalInbox(str(tmp_path))
    (tmp_path / "a.json").write_text(json.dumps(_PAYLOAD))
    sigs = inbox.poll()
    assert len(sigs) == 1
    assert sigs[0].symbol == "US.AAPL" and sigs[0].direction == "BUY"
    assert not (tmp_path / "a.json").exists()
    assert (tmp_path / "processed" / "a.json").exists()


def test_poll_moves_malformed_file_to_rejected_without_crashing(tmp_path):
    inbox = SignalInbox(str(tmp_path))
    (tmp_path / "bad.json").write_text("{not valid json")
    sigs = inbox.poll()
    assert sigs == []
    assert (tmp_path / "rejected" / "bad.json").exists()


def test_poll_empty_dir_returns_empty(tmp_path):
    inbox = SignalInbox(str(tmp_path))
    assert inbox.poll() == []


def test_poll_is_idempotent_across_runs(tmp_path):
    inbox = SignalInbox(str(tmp_path))
    (tmp_path / "a.json").write_text(json.dumps(_PAYLOAD))
    assert len(inbox.poll()) == 1
    assert inbox.poll() == []   # file already consumed -> nothing re-processed
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
python3 -m pytest tests/test_signal_inbox.py -v
```

Expected: FAIL with `ModuleNotFoundError: No module named 'autotrader.signals.inbox'`

- [ ] **Step 3: Create `autotrader/signals/inbox.py`**

```python
"""File-drop external-signal ingress — localhost-only by construction (a watched
directory has no network surface at all, the strongest reading of CLAUDE.md's
"no internet-reachable order path"). Drop a *.json RoutineSignalPayload into
inbox_dir; poll() validates + normalizes each into domain.Signal, then moves the
file to processed/ (ok) or rejected/ (bad). A malformed file is logged and
quarantined, never crashing the loop. Imports pydantic + domain only — no SDK."""
from __future__ import annotations

import logging
from pathlib import Path
from typing import List

from pydantic import ValidationError

from autotrader.domain import Signal
from autotrader.signals.normalize import normalize_payload
from autotrader.signals.schema import RoutineSignalPayload

logger = logging.getLogger("autotrader.signals.inbox")


class SignalInbox:
    def __init__(self, inbox_dir: str, confidence_scale: float = 10.0):
        self._dir = Path(inbox_dir)
        self._processed = self._dir / "processed"
        self._rejected = self._dir / "rejected"
        for d in (self._dir, self._processed, self._rejected):
            d.mkdir(parents=True, exist_ok=True)
        self._scale = confidence_scale

    def poll(self) -> List[Signal]:
        """Validate + normalize every top-level *.json file, moving each out of
        the inbox. Returns the flattened list of normalized signals."""
        signals: List[Signal] = []
        # sorted() makes processing order deterministic (filename-ordered);
        # glob("*.json") is non-recursive, so processed/ and rejected/ are skipped.
        for path in sorted(self._dir.glob("*.json")):
            try:
                payload = RoutineSignalPayload.model_validate_json(
                    path.read_text(encoding="utf-8"))
                signals.extend(normalize_payload(payload, self._scale))
            except (ValidationError, ValueError, OSError) as e:
                logger.warning("rejected signal file %s: %s", path.name, e)
                path.replace(self._rejected / path.name)
                continue
            path.replace(self._processed / path.name)
        return signals
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
python3 -m pytest tests/test_signal_inbox.py -v
```

Expected: 4 passed.

- [ ] **Step 5: Commit**

```bash
git add autotrader/signals/inbox.py tests/test_signal_inbox.py
git commit -m "feat: file-drop SignalInbox (localhost-only ingress, validate + quarantine)"
```

---

### Task 3: `TRAILING_STOP` order type — domain + brokers + router audit

**Files:**
- Modify: `autotrader/domain.py`
- Modify: `autotrader/sim_broker.py`
- Modify: `autotrader/moomoo_broker.py`
- Modify: `autotrader/router.py`
- Modify: `tests/test_domain.py`
- Modify: `tests/test_sim_broker.py`

- [ ] **Step 1: Write the failing domain tests**

Add at the bottom of `tests/test_domain.py`:

```python
def test_trailing_stop_order_requires_trail_percent_and_no_limit():
    r = OrderRequest(symbol="US.AAPL", side="SELL", qty=10, order_type="TRAILING_STOP",
                     limit_price=None, client_order_id="ts", trail_percent=5.0)
    assert r.trail_percent == 5.0
    with pytest.raises(ValueError):  # missing trail_percent
        OrderRequest(symbol="US.AAPL", side="SELL", qty=10, order_type="TRAILING_STOP",
                     limit_price=None, client_order_id="ts")
    with pytest.raises(ValueError):  # TRAILING_STOP must not carry a limit_price
        OrderRequest(symbol="US.AAPL", side="SELL", qty=10, order_type="TRAILING_STOP",
                     limit_price=99.0, client_order_id="ts", trail_percent=5.0)


def test_non_trailing_order_defaults_trail_percent_to_none():
    r = OrderRequest(symbol="US.AAPL", side="BUY", qty=1, order_type="MARKET",
                     limit_price=None, client_order_id="m")
    assert r.trail_percent is None
```

- [ ] **Step 2: Write the failing sim-broker tests**

Add at the bottom of `tests/test_sim_broker.py`:

```python
def _ts_req(cid="ts1", qty=10):
    return OrderRequest(symbol="US.AAPL", side="SELL", qty=qty,
                        order_type="TRAILING_STOP", limit_price=None,
                        client_order_id=cid, trail_percent=5.0)


def test_trailing_stop_rests_and_does_not_fill():
    b = SimBroker(quotes={"US.AAPL": 100.0}, cash=10000.0)  # auto_fill defaults True
    ack = b.place_order(_ts_req())
    assert ack.state is OrderState.SUBMITTED              # resting, NOT filled
    assert b.get_account().position_qty("US.AAPL") == 0   # no position change
    assert len(b.get_open_orders()) == 1                  # appears as a working order


def test_trailing_stop_cleared_by_cancel_all():
    b = SimBroker(quotes={"US.AAPL": 100.0}, cash=10000.0)
    b.place_order(_ts_req())
    assert len(b.get_open_orders()) == 1
    b.cancel_all()
    assert b.get_open_orders() == []
```

- [ ] **Step 3: Run both test files to verify the new tests fail**

```bash
python3 -m pytest tests/test_domain.py tests/test_sim_broker.py -v
```

Expected: the 2 new domain tests fail with `TypeError: ... unexpected keyword argument 'trail_percent'`; the 2 new sim-broker tests fail (a `TRAILING_STOP` currently auto-fills, so `state` is `FILLED` and `get_open_orders()` is empty).

- [ ] **Step 4: Extend `OrderType` + `OrderRequest` in `autotrader/domain.py`**

Change the `OrderType` alias (line 11):

```python
OrderType = Literal["MARKET", "LIMIT", "TRAILING_STOP"]
```

Replace the `OrderRequest` dataclass (the fields + `__post_init__`) with:

```python
@dataclass(frozen=True)
class OrderRequest:
    symbol: str
    side: Side
    qty: int
    order_type: OrderType
    limit_price: Optional[float]
    client_order_id: str
    trail_percent: Optional[float] = None   # required for TRAILING_STOP; else None

    def __post_init__(self):
        if self.side not in ("BUY", "SELL"):
            raise ValueError(f"OrderRequest.side must be BUY/SELL, got {self.side!r}")
        if self.qty <= 0:
            raise ValueError("OrderRequest.qty must be > 0")
        if self.order_type == "LIMIT" and self.limit_price is None:
            raise ValueError("LIMIT order requires limit_price")
        if self.order_type == "TRAILING_STOP":
            if self.trail_percent is None or self.trail_percent <= 0:
                raise ValueError("TRAILING_STOP order requires trail_percent > 0")
            if self.limit_price is not None:
                raise ValueError("TRAILING_STOP order must not set limit_price")
```

- [ ] **Step 5: Make `TRAILING_STOP` orders rest in `autotrader/sim_broker.py`**

Replace the body of `place_order` (the fill/rest branch) so a `TRAILING_STOP` always rests regardless of `auto_fill`:

```python
    def place_order(self, req: OrderRequest) -> OrderAck:
        if req.client_order_id in self._acks_by_cid:  # idempotency (R8)
            return self._acks_by_cid[req.client_order_id]
        self._seq += 1
        boid = f"sim-{self._seq}"
        price = req.limit_price or self._quotes.get(req.symbol, 0.0)
        # A TRAILING_STOP is a broker-RESTING protective order: it never fills
        # immediately, regardless of auto_fill (it waits for the trail to trigger).
        rests = req.order_type == "TRAILING_STOP" or not self._auto_fill
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

- [ ] **Step 6: Add the `TRAILING_STOP` branch to `MoomooBroker.place_order` (live path)**

In `autotrader/moomoo_broker.py`, replace `place_order` with the kwargs-building form that handles all three order types (the rate-limit guard stays first, so the offline rate-limit test is unaffected):

```python
    def place_order(self, req: OrderRequest) -> OrderAck:
        if not self._order_rl.acquire(timeout=60.0):
            raise BrokerError(BrokerErrorKind.RATE_LIMIT,
                               "order rate limit: timed out waiting for order token")
        side = self._c.TrdSide.BUY if req.side == "BUY" else self._c.TrdSide.SELL
        kwargs = dict(qty=int(req.qty), code=req.symbol, trd_side=side,
                      trd_env=self._env(), acc_id=self._acc_id,
                      remark=req.client_order_id[:64])  # idempotency key in remark (R8)
        if req.order_type == "MARKET":
            kwargs.update(price=0.0, order_type=self._c.OrderType.MARKET)
        elif req.order_type == "TRAILING_STOP":  # pragma: no cover — live OpenD path
            from moomoo import TrailType  # confined to this adapter
            kwargs.update(price=0.0, order_type=self._c.OrderType.TRAILING_STOP,
                          trail_type=TrailType.RATIO, trail_value=float(req.trail_percent))
        else:  # LIMIT
            kwargs.update(price=float(req.limit_price), order_type=self._c.OrderType.NORMAL)
        try:
            ret, data = self._trade.place_order(**kwargs)
        except Exception as e:  # socket drop / timeout -> UNKNOWN, never success
            return OrderAck(req.client_order_id, None, OrderState.UNKNOWN, {"error": str(e)})
        if not self._ok(ret):
            return OrderAck(req.client_order_id, None, OrderState.REJECTED, {"error": str(data)})
        row = data.iloc[0]
        boid = str(self._c.safe_get(row, "order_id", "orderID", default=""))
        return OrderAck(req.client_order_id, boid, OrderState.SUBMITTED, {})
```

- [ ] **Step 7: Add `trail_percent` to the router intent audit line**

In `autotrader/router.py`, in `submit()`, extend the `"intent"` audit payload to include `trail_percent`:

```python
            # AUDIT FIRST — if this raises, no order is placed (E18).
            self._audit("intent", {
                "client_order_id": req.client_order_id, "symbol": req.symbol,
                "side": req.side, "qty": req.qty, "order_type": req.order_type,
                "limit_price": req.limit_price, "trail_percent": req.trail_percent,
            })
```

- [ ] **Step 8: Run the affected test files**

```bash
python3 -m pytest tests/test_domain.py tests/test_sim_broker.py tests/test_router.py tests/test_moomoo_broker_offline.py -v
```

Expected: 8 passed (domain), 7 passed (sim broker), 5 passed (router — `trail_percent` is an extra key, the existing assertions only check `action`), 6 passed (moomoo offline — Task 3 adds no test here; the rate-limit guard runs before any new code so the existing 6 stay green).

- [ ] **Step 9: Commit**

```bash
git add autotrader/domain.py autotrader/sim_broker.py autotrader/moomoo_broker.py autotrader/router.py tests/test_domain.py tests/test_sim_broker.py
git commit -m "feat: first-class TRAILING_STOP order type (rests at broker, audited, live TrailType.RATIO)"
```

---

### Task 4: `RiskConfig.trailing_stop_pct`

**Files:**
- Modify: `autotrader/config.py`
- Modify: `config/risk.config.example`
- Modify: `tests/test_config.py`

- [ ] **Step 1: Write the failing test**

Add at the bottom of `tests/test_config.py`:

```python
def test_loader_reads_trailing_stop_pct(monkeypatch):
    monkeypatch.delenv("RISK_TRAILING_STOP_PCT", raising=False)
    assert load_risk_config().trailing_stop_pct == 0.0   # disabled by default
    monkeypatch.setenv("RISK_TRAILING_STOP_PCT", "5.0")
    assert load_risk_config().trailing_stop_pct == 5.0
```

- [ ] **Step 2: Run the test to verify it fails**

```bash
python3 -m pytest tests/test_config.py -k trailing_stop_pct -v
```

Expected: FAIL with `AttributeError: 'RiskConfig' object has no attribute 'trailing_stop_pct'`

- [ ] **Step 3: Add the field + loader to `autotrader/config.py`**

Add a new field as the LAST field of the `RiskConfig` dataclass (it has a default, so existing positional/keyword constructions across the suite stay valid):

```python
@dataclass(frozen=True)
class RiskConfig:
    trading_env: str            # "PAPER" | "LIVE" (LIVE also needs manual GUI unlock)
    min_confidence: float
    max_order_notional: float
    max_position_qty: int
    daily_loss_limit: float     # positive number; halt when day_pnl <= -limit
    max_gross_exposure: float
    allowed_symbols: FrozenSet[str]
    trailing_stop_pct: float = 0.0   # 0 disables broker-resting stops; e.g. 5.0 = 5%
```

In `load_risk_config()`, add the loader line to the returned `RiskConfig(...)` (after `allowed_symbols=symbols,`):

```python
        allowed_symbols=symbols,
        trailing_stop_pct=_f("RISK_TRAILING_STOP_PCT", 0.0),
    )
```

- [ ] **Step 4: Document the env var in `config/risk.config.example`**

Add after the `RISK_ALLOWED_SYMBOLS` line:

```bash
# Broker-resting trailing-stop percent attached on each BUY entry (Phase 2c).
# 0 disables; 5.0 = a 5% trailing stop that survives an OpenD outage (research R5).
# Risk-limit CHANGES require explicit human review (CLAUDE.md).
RISK_TRAILING_STOP_PCT=5.0
```

- [ ] **Step 5: Run the config tests**

```bash
python3 -m pytest tests/test_config.py -v
```

Expected: 4 passed.

- [ ] **Step 6: Commit**

```bash
git add autotrader/config.py config/risk.config.example tests/test_config.py
git commit -m "feat: RISK_TRAILING_STOP_PCT config (default 0 = disabled)"
```

---

### Task 5: Engine — shared `_route_signal`, external-signal entry, trailing-stop attach

**Files:**
- Modify: `autotrader/main.py` (`TradeEngine`)
- Modify: `tests/test_main_loop.py`

- [ ] **Step 1: Write the failing tests**

Add at the bottom of `tests/test_main_loop.py`:

```python
def test_submit_external_signal_routes_and_places(tmp_path):
    from autotrader.domain import Signal
    b = SimBroker(quotes={"US.AAPL": 101.0}, cash=100000.0)
    eng = _engine(b, tmp_path=tmp_path)   # no entry gate -> BUY allowed
    res = eng.submit_external_signal(
        Signal(symbol="US.AAPL", direction="BUY", confidence=0.9, rationale="ext"))
    assert res.action == "ORDER_PLACED"
    assert b.get_account().position_qty("US.AAPL") == 10


def test_submit_external_signal_blocked_by_entry_gate(tmp_path):
    from autotrader.domain import Signal
    from autotrader.lifecycle import EntryGate
    b = SimBroker(quotes={"US.AAPL": 101.0}, cash=100000.0)
    strat = ThresholdStrategy(StrategyParams(symbol="US.AAPL", entry_price=100.0,
                                             stop_loss_pct=0.05, take_profit_pct=0.10,
                                             confidence=0.7))
    eng = TradeEngine(broker=b, strategy=strat, cfg=_cfg(), order_qty=10,
                      audit_path=str(tmp_path / "audit.jsonl"),
                      entry_gate=EntryGate(enabled=False))
    res = eng.submit_external_signal(
        Signal(symbol="US.AAPL", direction="BUY", confidence=0.9, rationale="ext"))
    assert res.action == "ENTRY_CLOSED"
    assert b.get_account().position_qty("US.AAPL") == 0


def test_submit_external_signal_drops_low_confidence(tmp_path):
    from autotrader.domain import Signal
    b = SimBroker(quotes={"US.AAPL": 101.0}, cash=100000.0)
    eng = _engine(b, cfg=_cfg(min_confidence=0.9), tmp_path=tmp_path)
    res = eng.submit_external_signal(
        Signal(symbol="US.AAPL", direction="BUY", confidence=0.5, rationale="ext"))
    assert res.action == "DROPPED_LOW_CONFIDENCE"


def test_buy_entry_attaches_trailing_stop(tmp_path):
    from autotrader.db import DB
    db = DB(str(tmp_path / "stops.db"))
    b = SimBroker(quotes={"US.AAPL": 101.0}, cash=100000.0)
    strat = ThresholdStrategy(StrategyParams(symbol="US.AAPL", entry_price=100.0,
                                             stop_loss_pct=0.05, take_profit_pct=0.10,
                                             confidence=0.7))
    eng = TradeEngine(broker=b, strategy=strat, cfg=_cfg(trailing_stop_pct=5.0),
                      order_qty=10, audit_path=str(tmp_path / "audit.jsonl"), db=db)
    assert eng.tick().action == "ORDER_PLACED"
    assert b.get_account().position_qty("US.AAPL") == 10     # BUY filled
    assert len(b.get_open_orders()) == 1                     # the resting trailing stop
    row = db._conn.execute(
        "SELECT side, qty FROM trades WHERE order_type='TRAILING_STOP'").fetchone()
    assert row is not None and row[0] == "SELL" and row[1] == 10
    db.close()


def test_no_trailing_stop_when_disabled(tmp_path):
    b = SimBroker(quotes={"US.AAPL": 101.0}, cash=100000.0)
    eng = _engine(b, tmp_path=tmp_path)   # _cfg() default trailing_stop_pct=0.0
    assert eng.tick().action == "ORDER_PLACED"
    assert b.get_open_orders() == []      # no resting stop when disabled
```

- [ ] **Step 2: Run the new tests to verify they fail**

```bash
python3 -m pytest tests/test_main_loop.py -k "external_signal or trailing_stop" -v
```

Expected: FAIL — `submit_external_signal` does not exist (`AttributeError`), and the trailing-stop tests fail because no stop is attached yet.

- [ ] **Step 3: Import `Signal` into `autotrader/main.py`**

Change the domain import line (line 14) to also import `Signal`:

```python
from autotrader.domain import OrderRequest, OrderState, Signal
```

- [ ] **Step 4: Refactor `tick()` and add the new methods in `TradeEngine`**

Replace the whole `tick()` method with the thin form plus a shared `_route_signal()`, a new `submit_external_signal()`, and `_attach_trailing_stop()`. The order path is identical to before — it is just factored so both the strategy tick and external signals share it:

```python
    def tick(self) -> TickResult:
        snap = self._b.get_account()
        symbol = self._strat.p.symbol
        price = self._b.get_quote(symbol)
        if price is None:
            return TickResult("NO_QUOTE", symbol)

        pos = next((p for p in snap.positions if p.symbol == symbol), None)
        signal = self._strat.evaluate(price=price, position=pos)
        if signal is None:
            return TickResult("NO_SIGNAL")
        return self._route_signal(signal, snap, price)

    def submit_external_signal(self, signal: Signal) -> TickResult:
        """Route a validated external signal through the SAME pipeline as a
        strategy signal: confidence filter -> entry gate -> risk core -> router.
        External signals NEVER bypass the risk core (research C4)."""
        snap = self._b.get_account()
        price = self._b.get_quote(signal.symbol)
        if price is None:
            return TickResult("NO_QUOTE", signal.symbol)
        return self._route_signal(signal, snap, price)

    def _route_signal(self, signal: Signal, snap, price: float) -> TickResult:
        if signal.confidence < self._cfg.min_confidence:
            return TickResult("DROPPED_LOW_CONFIDENCE", f"{signal.confidence}")

        # Entry-window gate: block NEW entries (BUY) when closed; exits (SELL)
        # are never gated — you must always be able to flatten.
        if (signal.direction == "BUY" and self._gate is not None
                and not self._gate.entries_enabled):
            return TickResult("ENTRY_CLOSED", signal.symbol)

        if self._db:
            self._db.record_performance(
                day_pnl=snap.day_pnl,
                total_assets=snap.total_assets,
                cash=snap.cash,
                gross_exposure=snap.gross_exposure(),
            )

        self._signal_seq += 1
        signal_id = f"sig-{self._signal_seq}"
        if self._db:
            self._db.record_signal(
                symbol=signal.symbol, direction=signal.direction,
                confidence=signal.confidence, rationale=signal.rationale,
                signal_id=signal_id,
            )

        # SELL exits liquidate the full position; BUY uses the configured order_qty.
        pos = next((p for p in snap.positions if p.symbol == signal.symbol), None)
        eff_qty = pos.qty if (signal.direction == "SELL" and pos is not None) else self._qty
        cid = OrderRouter.make_client_order_id(signal.symbol, signal.direction, eff_qty, signal_id)
        req = OrderRequest(symbol=signal.symbol, side=signal.direction, qty=eff_qty,
                           order_type="MARKET", limit_price=None, client_order_id=cid)

        decision = evaluate(req, snap, self._cfg, ref_price=price)
        if not decision.approved:
            logger.warning("risk core rejected: %s", decision.reason)
            return TickResult("REJECTED_BY_RISK", decision.reason)

        ack = self._router.submit(req)
        if self._db:
            self._db.record_trade(
                client_order_id=ack.client_order_id, symbol=req.symbol, side=req.side,
                qty=req.qty, order_type=req.order_type, limit_price=req.limit_price,
                broker_order_id=ack.broker_order_id, state=ack.state.value,
            )
        if ack.state is OrderState.UNKNOWN:
            return TickResult("ORDER_UNKNOWN", ack.client_order_id)
        if ack.state is OrderState.REJECTED:
            return TickResult("ORDER_REJECTED", ack.client_order_id)

        # Broker-resting trailing stop: attach a protective TRAILING_STOP SELL
        # right after a BUY entry places (research R5 — survives an OpenD outage).
        if signal.direction == "BUY" and self._cfg.trailing_stop_pct > 0:
            self._attach_trailing_stop(signal.symbol, eff_qty, price, signal_id)
        return TickResult("ORDER_PLACED", str(ack.broker_order_id))

    def _attach_trailing_stop(self, symbol: str, qty: int, ref_price: float,
                              entry_signal_id: str) -> None:
        """Place a broker-resting TRAILING_STOP SELL for `qty` shares through the
        SAME audited risk path. Idempotent: the client_order_id is derived from
        the entry's signal id, so re-attaching for the same entry dedupes at the
        router. A rejected stop is logged, never fatal to the entry. Stop
        consolidation on qty changes (pyramiding) is Phase 3."""
        snap = self._b.get_account()  # reflect the just-placed position for the long-only check
        cid = OrderRouter.make_client_order_id(symbol, "SELL", qty, f"{entry_signal_id}-stop")
        req = OrderRequest(symbol=symbol, side="SELL", qty=qty, order_type="TRAILING_STOP",
                           limit_price=None, client_order_id=cid,
                           trail_percent=self._cfg.trailing_stop_pct)
        decision = evaluate(req, snap, self._cfg, ref_price=ref_price)
        if not decision.approved:
            logger.warning("trailing stop NOT attached for %s: %s", symbol, decision.reason)
            return
        ack = self._router.submit(req)
        if self._db:
            self._db.record_trade(
                client_order_id=ack.client_order_id, symbol=req.symbol, side=req.side,
                qty=req.qty, order_type=req.order_type, limit_price=req.limit_price,
                broker_order_id=ack.broker_order_id, state=ack.state.value,
            )
        logger.info("trailing stop attached: %s SELL %d @ %.1f%% trail",
                    symbol, qty, self._cfg.trailing_stop_pct)
```

- [ ] **Step 5: Run the full main-loop file**

```bash
python3 -m pytest tests/test_main_loop.py -v
```

Expected: 16 passed (11 existing — behavior unchanged — plus the 5 new).

- [ ] **Step 6: Commit**

```bash
git add autotrader/main.py tests/test_main_loop.py
git commit -m "feat: external-signal entry (submit_external_signal) + trailing-stop attach via shared _route_signal"
```

---

### Task 6: Wire `SignalInbox` into the runner and `main()`

**Files:**
- Modify: `autotrader/runner.py`
- Modify: `autotrader/main.py` (`main()`)
- Modify: `config/risk.config.example`
- Modify: `tests/test_runner.py`

- [ ] **Step 1: Write the failing tests**

In `tests/test_runner.py`, first update the `_build` helper to accept an optional inbox and pass it to `SessionRunner` (replace the existing `_build`):

```python
def _build(tmp_path, broker, *, healthy=True, gate_enabled=False, inbox=None):
    db = DB(str(tmp_path / "runner.db"))
    gate = EntryGate(enabled=gate_enabled)
    strat = ThresholdStrategy(StrategyParams(symbol="US.AAPL", entry_price=100.0,
                                             stop_loss_pct=0.05, take_profit_pct=0.10,
                                             confidence=0.7))
    eng = TradeEngine(broker=broker, strategy=strat, cfg=_cfg(), order_qty=10,
                      audit_path=str(tmp_path / "audit.jsonl"), db=db, entry_gate=gate)
    watch = Watchdog(health_check=lambda: healthy, reconcile=lambda: None,
                     sleep=lambda s: None)
    runner = SessionRunner(engine=eng, broker=broker, db=db, gate=gate,
                           scheduler=LifecycleScheduler(), watchdog=watch,
                           clock=FixedClock(_dt(8, 0)), sleep=lambda s: None,
                           signal_inbox=inbox)
    return runner, db, gate
```

Then add these two tests at the bottom of `tests/test_runner.py`:

```python
def test_run_once_routes_external_signal_from_inbox(tmp_path):
    import json
    from autotrader.signals.inbox import SignalInbox
    inbox_dir = tmp_path / "inbox"
    inbox_dir.mkdir()
    payload = {
        "routine_id": "r1", "timestamp": "2026-06-12T09:46:00-04:00",
        "signal_changes": [
            {"ticker": "AAPL", "direction": "UP", "transition": ["50", "200"],
             "points_delta": 10, "driver": "breakout"}
        ],
    }
    (inbox_dir / "sig1.json").write_text(json.dumps(payload))
    # Quote 99 < entry 100 -> the STRATEGY emits NO_SIGNAL; only the external BUY acts.
    b = SimBroker(quotes={"US.AAPL": 99.0}, cash=100000.0)
    runner, db, gate = _build(tmp_path, b, inbox=SignalInbox(str(inbox_dir)))
    runner.run_once(_dt(9, 46))   # ENTRY_OPEN fires -> gate opens -> external BUY routes
    assert b.get_account().position_qty("US.AAPL") == 10
    db.close()


def test_run_once_skips_inbox_when_unhealthy(tmp_path):
    import json
    from autotrader.signals.inbox import SignalInbox
    inbox_dir = tmp_path / "inbox"
    inbox_dir.mkdir()
    payload = {"routine_id": "r1", "timestamp": "2026-06-12T10:00:00-04:00",
               "signal_changes": [{"ticker": "AAPL", "direction": "UP",
                                   "transition": [], "points_delta": 10, "driver": "x"}]}
    (inbox_dir / "sig1.json").write_text(json.dumps(payload))
    b = SimBroker(quotes={"US.AAPL": 99.0}, cash=100000.0)
    runner, db, gate = _build(tmp_path, b, healthy=False, inbox=SignalInbox(str(inbox_dir)))
    assert runner.run_once(_dt(10, 0)) == "HALTED_UNHEALTHY"
    assert b.get_account().position_qty("US.AAPL") == 0   # no external order placed
    assert (inbox_dir / "sig1.json").exists()             # file NOT consumed while halted
    db.close()
```

- [ ] **Step 2: Run the runner tests to verify the new ones fail**

```bash
python3 -m pytest tests/test_runner.py -v
```

Expected: FAIL with `TypeError: SessionRunner.__init__() got an unexpected keyword argument 'signal_inbox'`

- [ ] **Step 3: Add `signal_inbox` to `SessionRunner` and poll it in `run_once`**

In `autotrader/runner.py`, update `__init__` to accept and store the optional inbox (add the parameter and assignment):

```python
    def __init__(self, engine, broker, db, gate: EntryGate,
                 scheduler: LifecycleScheduler, watchdog, clock: Clock,
                 sleep: Callable[[float], None], loop_interval: float = 5.0,
                 signal_inbox=None):
        self._engine = engine
        self._broker = broker
        self._db = db
        self._gate = gate
        self._sched = scheduler
        self._watch = watchdog
        self._clock = clock
        self._sleep = sleep
        self._loop_interval = loop_interval
        self._inbox = signal_inbox
```

Replace `run_once` so it polls the inbox and routes external signals ONLY when the watchdog is healthy (never trade blind — the inbox poll sits after the health gate):

```python
    def run_once(self, now) -> str:
        """Execute one loop iteration. Returns the engine tick action, or
        'HALTED_UNHEALTHY' if the watchdog could not restore the connection.
        External inbox signals are routed only when healthy."""
        for job in self._sched.poll(now):
            self._run_job(job)
        if not self._watch.ensure_healthy():
            return "HALTED_UNHEALTHY"
        action = self._engine.tick().action
        if self._inbox is not None:
            for sig in self._inbox.poll():
                res = self._engine.submit_external_signal(sig)
                logger.debug("external signal %s -> %s", sig.symbol, res.action)
        return action
```

- [ ] **Step 4: Run the runner tests to verify they pass**

```bash
python3 -m pytest tests/test_runner.py -v
```

Expected: 8 passed (6 existing + 2 new).

- [ ] **Step 5: Construct the inbox in `main()`**

In `autotrader/main.py`, inside `main()`, BEFORE the `runner = SessionRunner(...)` construction, add inbox setup:

```python
    inbox = None
    inbox_dir = os.getenv("AUTOTRADER_SIGNAL_INBOX")
    if inbox_dir:
        from autotrader.signals.inbox import SignalInbox
        inbox = SignalInbox(os.path.expanduser(inbox_dir))
        logger.info("external-signal inbox at %s", inbox_dir)
```

Then pass it to the runner — change the `SessionRunner(...)` call to add `signal_inbox=inbox`:

```python
    runner = SessionRunner(
        engine=engine, broker=broker, db=db, gate=gate,
        scheduler=LifecycleScheduler(), watchdog=watchdog, clock=Clock(),
        sleep=time.sleep,
        loop_interval=float(os.getenv("AUTOTRADER_LOOP_INTERVAL", "5")),
        signal_inbox=inbox,
    )
```

- [ ] **Step 6: Document the env var in `config/risk.config.example`**

Add at the bottom:

```bash
# External-signal file-drop inbox directory (Phase 2c). Unset = ingress disabled.
# Drop RoutineSignalPayload *.json files here; they route through the risk core.
# AUTOTRADER_SIGNAL_INBOX=~/.autotrader_inbox
```

- [ ] **Step 7: Verify `main.py` still imports cleanly**

```bash
python3 -c "import autotrader.main; print('main imports OK')"
python3 -m pytest tests/test_runner.py tests/test_main_loop.py -q
```

Expected: `main imports OK`, then 24 passed (16 main-loop + 8 runner).

- [ ] **Step 8: Commit**

```bash
git add autotrader/runner.py autotrader/main.py config/risk.config.example tests/test_runner.py
git commit -m "feat: wire SignalInbox into SessionRunner (route external signals when healthy) + main()"
```

---

### Task 7: Extend SDK-confinement guard to the signals layer + full suite

**Files:**
- Modify: `tests/test_no_sdk_in_core.py`

- [ ] **Step 1: Add the three `signals.*` modules to the confinement guard**

In `tests/test_no_sdk_in_core.py`, replace the module tuple in the `code` string so it also imports the new signals modules:

```python
    code = (
        "import importlib, sys\n"
        "for m in ('autotrader.domain', 'autotrader.config', 'autotrader.risk_core',\n"
        "          'autotrader.broker', 'autotrader.sim_broker', 'autotrader.router',\n"
        "          'autotrader.strategies.threshold', 'autotrader.main',\n"
        "          'autotrader.rate_limiter', 'autotrader.db',\n"
        "          'autotrader.clock', 'autotrader.scheduler', 'autotrader.lifecycle',\n"
        "          'autotrader.watchdog', 'autotrader.runner',\n"
        "          'autotrader.signals.schema', 'autotrader.signals.normalize',\n"
        "          'autotrader.signals.inbox',\n"
        "          'autotrader.moomoo_broker'):\n"
        "    importlib.import_module(m)\n"
        "assert 'moomoo' not in sys.modules, 'core import pulled in the moomoo SDK'\n"
        "print('ok')\n"
    )
```

- [ ] **Step 2: Run the confinement guard**

```bash
python3 -m pytest tests/test_no_sdk_in_core.py -v
```

Expected: 1 passed (importing `signals.schema`, `signals.normalize`, `signals.inbox` — which pull in pydantic, not moomoo — does NOT load the SDK).

- [ ] **Step 3: Run the full offline suite**

```bash
python3 -m pytest tests/ --ignore=tests/test_moomoo_broker_live.py -q
```

Expected: 118 passed, 0 skipped (baseline 97 + 21 new).

- [ ] **Step 4: Commit**

```bash
git add tests/test_no_sdk_in_core.py
git commit -m "test: extend SDK-confinement guard to the signals ingress layer"
```

---

## Self-Review

### 1. Spec coverage

Checking each Phase 2c item from roadmap §4:

| Roadmap 2c item | Covered by task |
|---|---|
| Pydantic external-signal ingress (`RoutineSignalPayload`), localhost-only | Task 1 (schema + normalizer) + Task 2 (file-drop `SignalInbox`, no network surface) ✓ |
| Normalized to `domain.Signal`, through confidence filter + risk core | Task 1 (`normalize_payload`) + Task 5 (`submit_external_signal` → shared `_route_signal`) + Task 6 (runner routes inbox signals when healthy) ✓ |
| Broker-resting `TRAILING_STOP` 5% attached on entry (R5) | Task 3 (domain/sim/moomoo/router `TRAILING_STOP`) + Task 4 (config) + Task 5 (`_attach_trailing_stop` after a BUY) ✓ |
| External signal path never bypasses the risk core (C4) | Task 5 (one `_route_signal` shared by `tick()` and `submit_external_signal`; the stop also routes through `evaluate` → router) ✓ |
| No internet-reachable order path | Task 2 (file-drop ingress — a watched directory, no listening socket) ✓ |
| Stop consolidation on qty change | **Deferred to Phase 3** (stated in scope note; roadmap §4.3) |
| `hard_stops` / catalyst execution | **Deferred** (validated + carried by the payload, not acted on — YAGNI) |
| Dashboard reads SQLite | **Deferred** (read-side wiring; projection now also records trailing-stop trades) |

### 2. Placeholder scan

No "TBD", "TODO", "implement later", or "similar to Task N" patterns. Every code step shows complete code; every command step shows the exact command and expected output.

### 3. Type consistency

- `OrderType = Literal["MARKET", "LIMIT", "TRAILING_STOP"]`; `OrderRequest.trail_percent: Optional[float] = None`, required+positive for `TRAILING_STOP`, forbidden with `limit_price` — used by `SimBroker.place_order`, `MoomooBroker.place_order`, `OrderRouter._audit`, and `TradeEngine._attach_trailing_stop` ✓
- `SignalChange(ticker, direction: Literal["UP","DOWN"], transition, points_delta, driver)`, `RoutineSignalPayload(routine_id, timestamp, signal_changes, hard_stops, catalysts)` — built identically in `schema.py`, `normalize.py`, `inbox.py`, and all tests ✓
- `normalize_change(change, confidence_scale=10.0) -> Signal`; `normalize_payload(payload, confidence_scale=10.0) -> List[Signal]` ✓
- `SignalInbox(inbox_dir, confidence_scale=10.0)`; `poll() -> List[Signal]` — used by the runner ✓
- `RiskConfig.trailing_stop_pct: float = 0.0` (last field, default) + loader `RISK_TRAILING_STOP_PCT`; read by `TradeEngine._route_signal`/`_attach_trailing_stop` ✓
- `TradeEngine.tick() -> TickResult`, `submit_external_signal(signal: Signal) -> TickResult`, `_route_signal(signal, snap, price) -> TickResult`, `_attach_trailing_stop(symbol, qty, ref_price, entry_signal_id) -> None` — `_route_signal` is the single shared order path ✓
- `SessionRunner(..., signal_inbox=None)`; `run_once(now) -> str` polls the inbox only inside the healthy branch ✓
- `Signal(symbol, direction, confidence, rationale)` from `domain` — produced by the normalizer, consumed by `submit_external_signal`; confidence stays in `[0,1]` via the clamp ✓

### 4. Determinism / safety invariants preserved

- The risk core is **unchanged** — `TRAILING_STOP` needs no edit (`_notional`/`eff_price` already fall back to `ref_price` for non-LIMIT orders, and a SELL stop passes the long-only check at `resulting = 0`). Both the strategy tick, the external signal, and the trailing stop flow through `risk_core.evaluate()` → `OrderRouter.submit()` (audit-first) — there is exactly one order path (C4, no new path).
- Ingress has **no network surface** (file-drop); a malformed payload is quarantined, never crashing the loop.
- External signals are gated by the watchdog (polled only when healthy) and the entry gate (BUY only inside the window); SELL exits and protective stops are never gated.
- No new module imports `moomoo`; the confinement guard is extended to prove the signals layer is SDK-free (Task 7).
- `trailing_stop_pct` defaults to `0.0`, so every pre-2c test (and the 2b runner tests) sees identical behavior; the field is a frozen `RiskConfig` member (human-reviewed to change).

---

Plan complete and saved to `docs/superpowers/plans/2026-06-13-autotrader-phase-2c-signals.md`.

**Two execution options:**

**1. Subagent-Driven (recommended)** — fresh subagent per task with two-stage spec + quality review after each.

**2. Inline Execution** — execute tasks in this session with checkpoints.

**Which approach?**
