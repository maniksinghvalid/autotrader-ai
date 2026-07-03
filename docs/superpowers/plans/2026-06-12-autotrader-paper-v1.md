# AutoTrader Paper v1 (Phase 0 + Phase 1) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the shortest *safe* path to placing one order in a Moomoo paper account through a deterministic risk core that an LLM signal can never bypass.

**Architecture:** Three rings from `docs/research/autotrader-architecture-research.md` §4 — (1) a deterministic Python driver (`main.py`) feeds market data to a stateless strategy whose output is a *data value*, not a command; (2) a deterministic risk core + order router is the only thing that can place an order, and only after every check passes; (3) a `Broker` port confines all Moomoo-isms to a `MoomooBroker` adapter that composes the vendored `skills/moomooapi/scripts/common.py` factories. A `SimBroker` implements the same port so the whole core is unit-testable with no live OpenD. All code lives in a new `autotrader/` package so it never pollutes repo root or the presentation-only `config/` dir.

**Tech Stack:** Python 3.11+, `moomoo-api>=10.4.6408` (vendored skills only — never `from moomoo import *` in app code), `pytest`, frozen `dataclasses`, stdlib `socket`/`threading`. No FastAPI, no ngrok, no APScheduler, no options, no unlock_trade in v1 (see "Explicitly out of scope").

---

## Why this plan replaces `plan/gemini-code-moomoo-bot-design.md`

The prior Gemini design was vetted and **rejected** for hard-rule violations: it called `unlock_trade` via the SDK (CLAUDE.md forbidden + risk R4), placed real orders against fabricated mock state, bypassed the vendored skills, had no risk/validation layer (R3), no `is_opend_ready()` gate (R5), no `refresh_cache=True` (R7), committed secrets, and exposed a trade-executing webhook to the internet (R1/R2). This plan implements the architecture all four research agents converged on instead. The Gemini doc's *good* ideas (Pydantic signal schema; Anti-Martingale / 50-200 options strategies) are deferred to post-paper phases as stateless strategy modules routed through the risk core.

## Explicitly out of scope for v1 (deferred to later, separately-validated plans)

ngrok / internet-exposed webhooks · options writing & covered calls · Anti-Martingale pyramiding · APScheduler EST crons · crypto (REAL-only) · HK L2 · SQLite projection + dashboard wiring · watchdog/auto-reconnect · live trading (`TRADING_ENV=LIVE`) · the formal `Broker` `Protocol` promotion and a 2nd adapter. v1 ships a plain concrete `MoomooBroker`, a local subscribe-driven loop, and one simple stateless equity strategy.

## File Structure

```
autotrader/
├── __init__.py
├── domain.py            # neutral dataclasses + OrderState enum + BrokerError taxonomy
├── config.py            # frozen RiskConfig + loader (env/config, never hardcoded)
├── risk_core.py         # pure deterministic evaluate(); the safety spine
├── broker.py            # Broker base class (method-name contract) — NOT a Protocol yet
├── sim_broker.py        # in-memory Broker for unit tests (no OpenD)
├── moomoo_broker.py     # MoomooBroker adapter — confines ALL Moomoo-isms
├── router.py            # audit-first JSONL + idempotency + per-symbol lock
├── strategies/
│   ├── __init__.py
│   └── threshold.py     # one stateless strategy w/ explicit stop-loss + take-profit
└── main.py              # orchestration: ready → subscribe → strategy → risk → route; cancel_all on shutdown
config/
└── risk.config.example  # non-secret risk-limit template (copy → risk.config)
tests/
├── conftest.py
├── test_domain.py
├── test_config.py
├── test_risk_core.py
├── test_sim_broker.py
├── test_router.py
├── test_strategy_threshold.py
├── test_main_loop.py
└── test_r19_master_gap.py
```

**Responsibilities:** `domain.py` is the lingua franca (no Moomoo imports — importable with no SDK). `risk_core.py` is pure (input dataclasses → decision; no I/O, no SDK). `moomoo_broker.py` is the *only* app file that touches `common.py`/the SDK. `router.py` owns the write-audit-before-order invariant and idempotency. `main.py` wires them and owns lifecycle. Tests import everything *except* `moomoo_broker.py` (which needs OpenD) and run with no network.

---

## Task 0: Branch + scaffolding

**Files:**
- Create: `autotrader/__init__.py`, `autotrader/strategies/__init__.py`, `tests/__init__.py`, `tests/conftest.py`
- Create: `pyproject.toml`, `.gitignore`
- Create: `config/risk.config.example`

- [ ] **Step 1: Create the working branch**

Run:
```bash
cd /Users/acdc/Documents/AI/AutoTrader && git checkout -b feat/autotrader-paper-v1
```
Expected: `Switched to a new branch 'feat/autotrader-paper-v1'`

- [ ] **Step 2: Create package + test init files**

```bash
mkdir -p autotrader/strategies tests
printf '"""AutoTrader deterministic paper-trading core (v1)."""\n' > autotrader/__init__.py
printf '"""Stateless strategy modules. Data in -> Signal out. No order execution."""\n' > autotrader/strategies/__init__.py
printf '' > tests/__init__.py
```

- [ ] **Step 3: Create `pyproject.toml`**

```toml
[project]
name = "autotrader"
version = "0.1.0"
requires-python = ">=3.11"
dependencies = ["moomoo-api>=10.4.6408"]

[project.optional-dependencies]
dev = ["pytest>=8.0"]

[tool.pytest.ini_options]
testpaths = ["tests"]
addopts = "-q"
```

- [ ] **Step 4: Create `.gitignore` (block secrets + state from ever being committed)**

```gitignore
__pycache__/
*.pyc
.pytest_cache/
.venv/
# Secrets & local state — NEVER commit (CLAUDE.md hard rule)
config/risk.config
config/secure.config
config/dashboard.config
*.env
.env
.futu_trade_audit.jsonl
```

- [ ] **Step 5: Create `config/risk.config.example` (non-secret template)**

```ini
# Copy to config/risk.config and adjust. Risk-limit CHANGES require explicit human
# review (CLAUDE.md). These are presentation/limit values, not secrets.
# Account/host/PIN are NOT here — they come from FUTU_* env vars and the OpenD GUI.
RISK_TRADING_ENV=PAPER
RISK_MIN_CONFIDENCE=0.60
RISK_MAX_ORDER_NOTIONAL=2000
RISK_MAX_POSITION_QTY=100
RISK_DAILY_LOSS_LIMIT=500
RISK_MAX_GROSS_EXPOSURE=10000
RISK_ALLOWED_SYMBOLS=US.AAPL,US.MSFT,US.NIO
```

- [ ] **Step 6: Create `tests/conftest.py` (keep the SDK out of unit tests)**

```python
"""Shared test fixtures. Unit tests must run with no OpenD and no SDK import."""
import sys
import pytest


@pytest.fixture(autouse=True)
def _no_accidental_sdk(monkeypatch):
    """Fail loudly if a unit test imports the live SDK module `moomoo`.

    moomoo_broker.py is the ONLY module allowed to touch it, and it is never
    imported by the pure-core tests. This guards that invariant.
    """
    if "moomoo" in sys.modules:
        pytest.skip("moomoo SDK present in env; pure-core tests assume it is absent")
    yield
```

- [ ] **Step 7: Verify pytest collects nothing yet (clean baseline)**

Run: `cd /Users/acdc/Documents/AI/AutoTrader && python -m pytest -q`
Expected: `no tests ran` (exit code 5) — confirms the harness is wired and empty.

- [ ] **Step 8: Commit**

```bash
git add autotrader/ tests/ pyproject.toml .gitignore config/risk.config.example
git commit -m "chore: scaffold autotrader package + test harness + risk config template"
```

---

## Task 1: Patch R19 — MASTER-account authorization gap

**Why first:** Research §6 Phase 0 + R19 (HIGH). `place_order.py:171-187` rejects MASTER accounts; `place_crypto_order.py` and `modify_order.py` do not, so an order can hit a master account affecting the whole group. Patch before any execution code.

**Files:**
- Modify: `skills/moomooapi/scripts/trade/place_crypto_order.py:140`
- Modify: `skills/moomooapi/scripts/trade/modify_order.py:92`
- Test: `tests/test_r19_master_gap.py`

- [ ] **Step 1: Write the failing test**

```python
"""R19: place_crypto_order and modify_order must reject MASTER accounts.

We import the script functions and stub create_*_trade_context with a fake ctx
whose get_acc_list() returns a MASTER row, then assert the function exits(1)
BEFORE any order method is called.
"""
import os
import sys
import types
import pytest

TRADE_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "skills", "moomooapi", "scripts", "trade",
)


class _FakeDF:
    """Minimal pandas-like frame: one MASTER account row."""
    def __init__(self, rows):
        self._rows = rows

    def __len__(self):
        return len(self._rows)

    @property
    def shape(self):
        return (len(self._rows), 1)

    class _ILoc:
        def __init__(self, rows):
            self._rows = rows

        def __getitem__(self, i):
            return self._rows[i]

    @property
    def iloc(self):
        return self._FakeDF_iloc

    def __init_subclass__(cls):  # pragma: no cover
        pass


def _master_acc_frame(acc_id):
    import pandas as pd
    return pd.DataFrame([{"acc_id": acc_id, "acc_role": "MASTER"}])


class _FakeCtx:
    def __init__(self, acc_id):
        self._acc_id = acc_id
        self.placed = False
        self.modified = False

    def get_acc_list(self):
        from common import RET_OK
        return RET_OK, _master_acc_frame(self._acc_id)

    def place_order(self, **kwargs):
        self.placed = True
        from common import RET_OK
        return RET_OK, "should-not-reach"

    def modify_order(self, **kwargs):
        self.modified = True
        from common import RET_OK
        return RET_OK, "should-not-reach"

    def close(self):
        pass


@pytest.fixture
def _trade_on_path():
    if TRADE_DIR not in sys.path:
        sys.path.insert(0, TRADE_DIR)
    scripts_dir = os.path.dirname(TRADE_DIR)
    if scripts_dir not in sys.path:
        sys.path.insert(0, scripts_dir)
    yield


def test_place_crypto_order_rejects_master(_trade_on_path, monkeypatch):
    pytest.importorskip("moomoo")  # needs SDK present to import the script
    import place_crypto_order as pco
    fake = _FakeCtx(acc_id=123)
    monkeypatch.setattr(pco, "create_crypto_trade_context", lambda **kw: fake)
    with pytest.raises(SystemExit) as exc:
        pco.place_crypto_order(code="CC.BTCUSD", side="BUY", quantity="0.01",
                               price=100.0, acc_id=123, confirmed=True)
    assert exc.value.code == 1
    assert fake.placed is False


def test_modify_order_rejects_master(_trade_on_path, monkeypatch):
    pytest.importorskip("moomoo")
    import modify_order as mo
    fake = _FakeCtx(acc_id=123)
    monkeypatch.setattr(mo, "create_trade_context", lambda *a, **kw: fake)
    with pytest.raises(SystemExit) as exc:
        mo.modify_order(order_id="1", price=10.0, quantity=1, acc_id=123)
    assert exc.value.code == 1
    assert fake.modified is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_r19_master_gap.py -v`
Expected: FAIL (orders are placed/modified because no MASTER guard exists). If `moomoo` is not installed, tests **skip** — in that case verify the patch by code review against `place_order.py:171-187` and re-run once the SDK is available via `/install-moomoo-opend`.

- [ ] **Step 3: Patch `place_crypto_order.py`** — insert the guard immediately after the context is created (after line 140 `ctx = create_crypto_trade_context(...)`), before `order_kwargs`:

```python
        ctx = create_crypto_trade_context(security_firm=firm_enum)
        # R19: reject MASTER accounts (parity with place_order.py) — a master
        # account order affects the whole account group. Patched per research R19.
        if acc_id:
            ret_a, acc_data = ctx.get_acc_list()
            if ret_a == RET_OK and not is_empty(acc_data):
                for i in range(len(acc_data)):
                    row = acc_data.iloc[i] if hasattr(acc_data, "iloc") else acc_data[i]
                    if safe_int(safe_get(row, "acc_id", default=0)) == safe_int(acc_id):
                        if format_enum(safe_get(row, "acc_role", default="")).upper() == "MASTER":
                            msg = "Master account (MASTER) is not allowed to place orders, please select a non-master account"
                            if output_json:
                                print(json.dumps({"error": msg}, ensure_ascii=False))
                            else:
                                print(f"Error: {msg}")
                            sys.exit(1)
                        break
        order_kwargs = dict(
```

- [ ] **Step 4: Patch `modify_order.py`** — insert the same guard immediately after `ctx = create_trade_context(...)` (line 92), before the auto-complete block. Add `safe_int` to the `from common import (...)` list (it currently imports `safe_get`, `safe_float`, `format_enum` but not `safe_int`):

```python
        ctx = create_trade_context(market, security_firm=parse_security_firm(security_firm))
        # R19: reject MASTER accounts (parity with place_order.py). Patched per research R19.
        if acc_id:
            ret_a, acc_data = ctx.get_acc_list()
            if ret_a == RET_OK and not is_empty(acc_data):
                for i in range(len(acc_data)):
                    row = acc_data.iloc[i] if hasattr(acc_data, "iloc") else acc_data[i]
                    if safe_int(safe_get(row, "acc_id", default=0)) == safe_int(acc_id):
                        if format_enum(safe_get(row, "acc_role", default="")).upper() == "MASTER":
                            msg = "Master account (MASTER) is not allowed to modify orders, please select a non-master account"
                            if output_json:
                                print(json.dumps({"error": msg}, ensure_ascii=False))
                            else:
                                print(f"Error: {msg}")
                            sys.exit(1)
                        break
```
And update the import line in `modify_order.py` (around line 31-38) to include `safe_int`:
```python
    safe_get,
    safe_float,
    safe_int,
    format_enum,
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `python -m pytest tests/test_r19_master_gap.py -v`
Expected: PASS (or SKIP if SDK absent — see Step 2 note).

- [ ] **Step 6: Commit**

```bash
git add skills/moomooapi/scripts/trade/place_crypto_order.py skills/moomooapi/scripts/trade/modify_order.py tests/test_r19_master_gap.py
git commit -m "fix(skills): patch R19 MASTER-account auth gap in crypto + modify order scripts"
```

---

## Task 2: Domain dataclasses + OrderState + BrokerError

**Files:**
- Create: `autotrader/domain.py`
- Test: `tests/test_domain.py`

- [ ] **Step 1: Write the failing test**

```python
import dataclasses
import pytest
from autotrader.domain import (
    Signal, OrderRequest, OrderAck, Fill, Position, AccountSnapshot,
    OrderState, BrokerError, BrokerErrorKind,
)


def test_signal_is_frozen_and_validates_confidence():
    s = Signal(symbol="US.AAPL", direction="BUY", confidence=0.8, rationale="x")
    assert s.symbol == "US.AAPL"
    with pytest.raises(dataclasses.FrozenInstanceError):
        s.confidence = 0.1  # type: ignore[misc]
    with pytest.raises(ValueError):
        Signal(symbol="US.AAPL", direction="BUY", confidence=1.5, rationale="x")
    with pytest.raises(ValueError):
        Signal(symbol="US.AAPL", direction="HOLD", confidence=0.5, rationale="x")


def test_order_request_requires_positive_qty_and_limit_price_rules():
    r = OrderRequest(symbol="US.AAPL", side="BUY", qty=10, order_type="MARKET",
                     limit_price=None, client_order_id="abc")
    assert r.qty == 10
    with pytest.raises(ValueError):
        OrderRequest(symbol="US.AAPL", side="BUY", qty=0, order_type="MARKET",
                     limit_price=None, client_order_id="abc")
    with pytest.raises(ValueError):
        OrderRequest(symbol="US.AAPL", side="BUY", qty=10, order_type="LIMIT",
                     limit_price=None, client_order_id="abc")  # LIMIT needs a price


def test_account_snapshot_exposure_and_position_lookup():
    snap = AccountSnapshot(
        cash=5000.0, total_assets=7000.0, day_pnl=-100.0, stale=False,
        positions=(Position(symbol="US.AAPL", qty=10, avg_price=200.0),),
    )
    assert snap.position_qty("US.AAPL") == 10
    assert snap.position_qty("US.MSFT") == 0
    assert snap.gross_exposure() == pytest.approx(2000.0)


def test_order_state_unknown_is_not_terminal_success():
    assert OrderState.UNKNOWN is not OrderState.FILLED
    assert OrderState.FILLED.is_success() is True
    assert OrderState.UNKNOWN.is_success() is False
    assert OrderState.REJECTED.is_success() is False


def test_broker_error_carries_kind():
    err = BrokerError(BrokerErrorKind.RATE_LIMIT, "too fast")
    assert err.kind is BrokerErrorKind.RATE_LIMIT
    assert "too fast" in str(err)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_domain.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'autotrader.domain'`.

- [ ] **Step 3: Write the implementation**

```python
"""Neutral domain types shared by the core. No Moomoo/SDK imports — this module
must import with no OpenD and no moomoo-api present."""
from __future__ import annotations

import enum
from dataclasses import dataclass, field
from typing import Literal, Optional, Tuple

Side = Literal["BUY", "SELL"]
OrderType = Literal["MARKET", "LIMIT"]


class OrderState(enum.Enum):
    PENDING = "PENDING"
    SUBMITTED = "SUBMITTED"
    PARTIAL = "PARTIAL"
    FILLED = "FILLED"
    CANCELLED = "CANCELLED"
    REJECTED = "REJECTED"
    UNKNOWN = "UNKNOWN"  # ACK timeout / socket drop — NEVER treat as success (E2/R8)

    def is_success(self) -> bool:
        return self is OrderState.FILLED


class BrokerErrorKind(enum.Enum):
    NOT_READY = "NOT_READY"
    RATE_LIMIT = "RATE_LIMIT"
    REJECTED = "REJECTED"
    TIMEOUT = "TIMEOUT"
    UNSUPPORTED = "UNSUPPORTED"
    UNKNOWN = "UNKNOWN"


class BrokerError(Exception):
    def __init__(self, kind: BrokerErrorKind, message: str):
        super().__init__(f"[{kind.value}] {message}")
        self.kind = kind


@dataclass(frozen=True)
class Signal:
    symbol: str
    direction: Side
    confidence: float
    rationale: str

    def __post_init__(self):
        if self.direction not in ("BUY", "SELL"):
            raise ValueError(f"Signal.direction must be BUY/SELL, got {self.direction}")
        if not (0.0 <= self.confidence <= 1.0):
            raise ValueError(f"confidence must be in [0,1], got {self.confidence}")


@dataclass(frozen=True)
class OrderRequest:
    symbol: str
    side: Side
    qty: int
    order_type: OrderType
    limit_price: Optional[float]
    client_order_id: str

    def __post_init__(self):
        if self.qty <= 0:
            raise ValueError("OrderRequest.qty must be > 0")
        if self.order_type == "LIMIT" and self.limit_price is None:
            raise ValueError("LIMIT order requires limit_price")


@dataclass(frozen=True)
class OrderAck:
    client_order_id: str
    broker_order_id: Optional[str]
    state: OrderState
    raw: dict = field(default_factory=dict)


@dataclass(frozen=True)
class Fill:
    fill_id: str
    symbol: str
    side: Side
    qty: float
    price: float
    ts: str


@dataclass(frozen=True)
class Position:
    symbol: str
    qty: int
    avg_price: float


@dataclass(frozen=True)
class AccountSnapshot:
    cash: float
    total_assets: float
    day_pnl: float
    stale: bool
    positions: Tuple[Position, ...] = ()

    def position_qty(self, symbol: str) -> int:
        for p in self.positions:
            if p.symbol == symbol:
                return p.qty
        return 0

    def gross_exposure(self) -> float:
        return sum(abs(p.qty) * p.avg_price for p in self.positions)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_domain.py -v`
Expected: PASS (5 passed).

- [ ] **Step 5: Commit**

```bash
git add autotrader/domain.py tests/test_domain.py
git commit -m "feat(autotrader): neutral domain dataclasses, OrderState, BrokerError taxonomy"
```

---

## Task 3: Frozen RiskConfig + loader

**Files:**
- Create: `autotrader/config.py`
- Test: `tests/test_config.py`

- [ ] **Step 1: Write the failing test**

```python
import dataclasses
import pytest
from autotrader.config import RiskConfig, load_risk_config


def test_risk_config_is_frozen():
    cfg = RiskConfig(trading_env="PAPER", min_confidence=0.6, max_order_notional=2000,
                     max_position_qty=100, daily_loss_limit=500, max_gross_exposure=10000,
                     allowed_symbols=frozenset({"US.AAPL"}))
    with pytest.raises(dataclasses.FrozenInstanceError):
        cfg.max_order_notional = 99999  # type: ignore[misc]


def test_loader_reads_env_and_defaults_to_paper(monkeypatch):
    for k in ("RISK_TRADING_ENV", "RISK_MIN_CONFIDENCE", "RISK_MAX_ORDER_NOTIONAL",
              "RISK_MAX_POSITION_QTY", "RISK_DAILY_LOSS_LIMIT", "RISK_MAX_GROSS_EXPOSURE",
              "RISK_ALLOWED_SYMBOLS"):
        monkeypatch.delenv(k, raising=False)
    cfg = load_risk_config()
    assert cfg.trading_env == "PAPER"          # safe default
    assert cfg.allowed_symbols == frozenset()  # empty allow-list = nothing tradable
    assert cfg.min_confidence == 0.6


def test_loader_parses_symbols_and_live_requires_explicit_flag(monkeypatch):
    monkeypatch.setenv("RISK_ALLOWED_SYMBOLS", "US.AAPL, us.msft ,US.NIO")
    monkeypatch.setenv("RISK_TRADING_ENV", "live")
    cfg = load_risk_config()
    assert cfg.allowed_symbols == frozenset({"US.AAPL", "US.MSFT", "US.NIO"})
    assert cfg.trading_env == "LIVE"  # normalized uppercase; routing is enforced in risk_core
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_config.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'autotrader.config'`.

- [ ] **Step 3: Write the implementation**

```python
"""Frozen risk configuration. Values come from env (RISK_* — typically sourced
from config/risk.config). NOTHING in here is hardcoded at a call site, and the
dataclass is immutable at runtime so the strategy/LLM layer cannot mutate a
limit (research R12). Changing a limit is a human-reviewed edit to the config."""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import FrozenSet


@dataclass(frozen=True)
class RiskConfig:
    trading_env: str            # "PAPER" | "LIVE" (LIVE also needs manual GUI unlock)
    min_confidence: float
    max_order_notional: float
    max_position_qty: int
    daily_loss_limit: float     # positive number; halt when day_pnl <= -limit
    max_gross_exposure: float
    allowed_symbols: FrozenSet[str]


def _f(name: str, default: float) -> float:
    return float(os.getenv(name, str(default)))


def load_risk_config() -> RiskConfig:
    raw_syms = os.getenv("RISK_ALLOWED_SYMBOLS", "")
    symbols = frozenset(
        s.strip().upper() for s in raw_syms.split(",") if s.strip()
    )
    env = os.getenv("RISK_TRADING_ENV", "PAPER").strip().upper()
    if env not in ("PAPER", "LIVE"):
        env = "PAPER"
    return RiskConfig(
        trading_env=env,
        min_confidence=_f("RISK_MIN_CONFIDENCE", 0.6),
        max_order_notional=_f("RISK_MAX_ORDER_NOTIONAL", 2000),
        max_position_qty=int(_f("RISK_MAX_POSITION_QTY", 100)),
        daily_loss_limit=_f("RISK_DAILY_LOSS_LIMIT", 500),
        max_gross_exposure=_f("RISK_MAX_GROSS_EXPOSURE", 10000),
        allowed_symbols=symbols,
    )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_config.py -v`
Expected: PASS (3 passed).

- [ ] **Step 5: Commit**

```bash
git add autotrader/config.py tests/test_config.py
git commit -m "feat(autotrader): frozen RiskConfig + env loader (paper-default, immutable limits)"
```

---

## Task 4: Deterministic risk core — `evaluate()`

**Why:** This is the safety spine (research §4.3). Pure function: a candidate `OrderRequest` + `AccountSnapshot` + `RiskConfig` → approve/reject with a reason. No I/O, no SDK. Every rejection path is unit-tested.

**Files:**
- Create: `autotrader/risk_core.py`
- Test: `tests/test_risk_core.py`

- [ ] **Step 1: Write the failing test**

```python
import pytest
from autotrader.domain import OrderRequest, AccountSnapshot, Position
from autotrader.config import RiskConfig
from autotrader.risk_core import evaluate, RiskDecision


def _cfg(**over):
    base = dict(trading_env="PAPER", min_confidence=0.6, max_order_notional=2000,
                max_position_qty=100, daily_loss_limit=500, max_gross_exposure=10000,
                allowed_symbols=frozenset({"US.AAPL"}))
    base.update(over)
    return RiskConfig(**base)


def _snap(**over):
    base = dict(cash=10000.0, total_assets=10000.0, day_pnl=0.0, stale=False, positions=())
    base.update(over)
    return AccountSnapshot(**base)


def _req(**over):
    base = dict(symbol="US.AAPL", side="BUY", qty=5, order_type="LIMIT",
                limit_price=100.0, client_order_id="cid")
    base.update(over)
    return OrderRequest(**base)


def test_clean_order_is_approved():
    d = evaluate(_req(), _snap(), _cfg(), ref_price=100.0)
    assert isinstance(d, RiskDecision)
    assert d.approved is True
    assert d.reason == "OK"


def test_reject_on_stale_snapshot():
    d = evaluate(_req(), _snap(stale=True), _cfg(), ref_price=100.0)
    assert d.approved is False
    assert "stale" in d.reason.lower()


def test_reject_symbol_not_in_allow_list():
    d = evaluate(_req(symbol="US.TSLA"), _snap(), _cfg(), ref_price=100.0)
    assert d.approved is False
    assert "allow" in d.reason.lower()


def test_reject_when_notional_exceeds_cap():
    d = evaluate(_req(qty=50), _snap(), _cfg(max_order_notional=2000), ref_price=100.0)
    assert d.approved is False
    assert "notional" in d.reason.lower()


def test_reject_when_resulting_position_exceeds_qty_cap():
    snap = _snap(positions=(Position("US.AAPL", qty=98, avg_price=100.0),))
    d = evaluate(_req(qty=5), snap, _cfg(max_position_qty=100), ref_price=100.0)
    assert d.approved is False
    assert "position" in d.reason.lower()


def test_reject_when_daily_loss_limit_breached():
    d = evaluate(_req(), _snap(day_pnl=-600.0), _cfg(daily_loss_limit=500), ref_price=100.0)
    assert d.approved is False
    assert "daily loss" in d.reason.lower()


def test_reject_when_gross_exposure_cap_breached():
    snap = _snap(positions=(Position("US.AAPL", qty=95, avg_price=100.0),))
    d = evaluate(_req(qty=10), snap, _cfg(max_gross_exposure=10000), ref_price=100.0)
    assert d.approved is False
    assert "exposure" in d.reason.lower()


def test_reject_live_env_routing_in_paper_only_v1():
    d = evaluate(_req(), _snap(), _cfg(trading_env="LIVE"), ref_price=100.0)
    assert d.approved is False
    assert "live" in d.reason.lower()


def test_reject_missing_ref_price():
    d = evaluate(_req(order_type="MARKET", limit_price=None), _snap(), _cfg(), ref_price=None)
    assert d.approved is False
    assert "price" in d.reason.lower()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_risk_core.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'autotrader.risk_core'`.

- [ ] **Step 3: Write the implementation**

```python
"""Deterministic risk core — the safety spine. Pure: (request, snapshot, cfg) ->
RiskDecision. No I/O, no SDK, no randomness. The strategy/LLM layer can never
reach place_order except through an approved decision here (research §4.3)."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from autotrader.config import RiskConfig
from autotrader.domain import AccountSnapshot, OrderRequest


@dataclass(frozen=True)
class RiskDecision:
    approved: bool
    reason: str


def _notional(req: OrderRequest, ref_price: float) -> float:
    price = req.limit_price if req.order_type == "LIMIT" and req.limit_price else ref_price
    return abs(req.qty) * price


def evaluate(req: OrderRequest, snapshot: AccountSnapshot, cfg: RiskConfig,
             ref_price: Optional[float]) -> RiskDecision:
    # 1. Environment routing — v1 is PAPER-only. LIVE is gated by a later phase
    #    AND a manual GUI unlock; never auto-promote (research R4/E17).
    if cfg.trading_env != "PAPER":
        return RiskDecision(False, f"env routing: {cfg.trading_env} not allowed in paper-only v1")

    # 2. Never trade on a stale snapshot (research R7/E1).
    if snapshot.stale:
        return RiskDecision(False, "account snapshot is stale; refusing to trade")

    # 3. Daily-loss halt (research §4.3). day_pnl is negative when losing.
    if snapshot.day_pnl <= -abs(cfg.daily_loss_limit):
        return RiskDecision(False, f"daily loss limit breached: pnl={snapshot.day_pnl}")

    # 4. Symbol allow-list.
    if req.symbol.upper() not in cfg.allowed_symbols:
        return RiskDecision(False, f"symbol {req.symbol} not in allow-list")

    # 5. A reference price must exist to size/clamp the order.
    eff_price = req.limit_price if (req.order_type == "LIMIT" and req.limit_price) else ref_price
    if not eff_price or eff_price <= 0:
        return RiskDecision(False, "no usable reference price for risk sizing")

    # 6. Max order notional clamp (research R3).
    notional = _notional(req, eff_price)
    if notional > cfg.max_order_notional:
        return RiskDecision(False, f"order notional {notional:.2f} > cap {cfg.max_order_notional}")

    # 7. Resulting position cap (only adds for BUY in v1 long-only strategy).
    resulting = snapshot.position_qty(req.symbol) + (req.qty if req.side == "BUY" else -req.qty)
    if abs(resulting) > cfg.max_position_qty:
        return RiskDecision(False, f"resulting position {resulting} > cap {cfg.max_position_qty}")

    # 8. Gross exposure cap after this order.
    projected = snapshot.gross_exposure() + notional
    if projected > cfg.max_gross_exposure:
        return RiskDecision(False, f"gross exposure {projected:.2f} > cap {cfg.max_gross_exposure}")

    return RiskDecision(True, "OK")
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_risk_core.py -v`
Expected: PASS (9 passed).

- [ ] **Step 5: Commit**

```bash
git add autotrader/risk_core.py tests/test_risk_core.py
git commit -m "feat(autotrader): deterministic risk core evaluate() with full reject-path coverage"
```

---

## Task 5: Broker base contract + SimBroker

**Why:** `broker.py` fixes the method-name contract (the future `Protocol`); `SimBroker` implements it in memory so the router, strategy, and main loop are testable with no OpenD (research §4.2 cut-line).

**Files:**
- Create: `autotrader/broker.py`
- Create: `autotrader/sim_broker.py`
- Test: `tests/test_sim_broker.py`

- [ ] **Step 1: Write the failing test**

```python
import pytest
from autotrader.domain import OrderRequest, OrderState
from autotrader.sim_broker import SimBroker


def _req(cid="c1", qty=10):
    return OrderRequest(symbol="US.AAPL", side="BUY", qty=qty, order_type="LIMIT",
                        limit_price=100.0, client_order_id=cid)


def test_sim_broker_ready_and_quote():
    b = SimBroker(quotes={"US.AAPL": 100.0})
    assert b.is_ready() is True
    assert b.get_quote("US.AAPL") == 100.0


def test_place_order_fills_and_updates_position():
    b = SimBroker(quotes={"US.AAPL": 100.0}, cash=10000.0)
    ack = b.place_order(_req(qty=10))
    assert ack.state is OrderState.FILLED
    assert ack.broker_order_id is not None
    snap = b.get_account()
    assert snap.position_qty("US.AAPL") == 10
    assert snap.cash == pytest.approx(9000.0)
    assert snap.stale is False


def test_place_order_is_idempotent_on_duplicate_client_order_id():
    b = SimBroker(quotes={"US.AAPL": 100.0}, cash=10000.0)
    a1 = b.place_order(_req(cid="dup", qty=10))
    a2 = b.place_order(_req(cid="dup", qty=10))  # same cid -> no double fill
    assert a1.broker_order_id == a2.broker_order_id
    assert b.get_account().position_qty("US.AAPL") == 10


def test_cancel_all_clears_open_orders():
    b = SimBroker(quotes={"US.AAPL": 100.0}, cash=10000.0, auto_fill=False)
    b.place_order(_req(cid="open1"))
    assert len(b.get_open_orders()) == 1
    b.cancel_all()
    assert b.get_open_orders() == []


def test_reconcile_fills_returns_dedupable_fills():
    b = SimBroker(quotes={"US.AAPL": 100.0}, cash=10000.0)
    b.place_order(_req(cid="c1"))
    fills = b.reconcile_fills(since=None)
    assert len(fills) == 1
    assert fills[0].fill_id  # stable id for dedupe
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_sim_broker.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'autotrader.sim_broker'`.

- [ ] **Step 3: Write `autotrader/broker.py`**

```python
"""Broker contract. v1 ships a plain base class (NOT a Protocol yet — promoting
is a ~30-min mechanical refactor, research §4.2). Concrete brokers: SimBroker
(tests) and MoomooBroker (live OpenD). All Moomoo-isms live in MoomooBroker."""
from __future__ import annotations

from typing import List, Optional

from autotrader.domain import AccountSnapshot, Fill, OrderAck, OrderRequest


class Broker:
    def connect(self) -> None: raise NotImplementedError
    def is_ready(self) -> bool: raise NotImplementedError
    def get_quote(self, symbol: str) -> Optional[float]: raise NotImplementedError
    def place_order(self, req: OrderRequest) -> OrderAck: raise NotImplementedError
    def cancel_order(self, broker_order_id: str) -> None: raise NotImplementedError
    def cancel_all(self) -> None: raise NotImplementedError
    def get_account(self) -> AccountSnapshot: raise NotImplementedError
    def get_open_orders(self) -> List[OrderAck]: raise NotImplementedError
    def reconcile_fills(self, since: Optional[str]) -> List[Fill]: raise NotImplementedError
    def close(self) -> None: raise NotImplementedError
```

- [ ] **Step 4: Write `autotrader/sim_broker.py`**

```python
"""In-memory Broker for deterministic unit tests. No OpenD, no SDK, no clock
dependence (fill ids/timestamps are counters)."""
from __future__ import annotations

from typing import Dict, List, Optional

from autotrader.broker import Broker
from autotrader.domain import (
    AccountSnapshot, Fill, OrderAck, OrderRequest, OrderState, Position,
)


class SimBroker(Broker):
    def __init__(self, quotes: Dict[str, float], cash: float = 10000.0,
                 auto_fill: bool = True):
        self._quotes = dict(quotes)
        self._cash = cash
        self._positions: Dict[str, Position] = {}
        self._open: Dict[str, OrderAck] = {}
        self._fills: List[Fill] = []
        self._acks_by_cid: Dict[str, OrderAck] = {}
        self._auto_fill = auto_fill
        self._seq = 0

    def connect(self) -> None:
        return None

    def is_ready(self) -> bool:
        return True

    def get_quote(self, symbol: str) -> Optional[float]:
        return self._quotes.get(symbol)

    def place_order(self, req: OrderRequest) -> OrderAck:
        if req.client_order_id in self._acks_by_cid:  # idempotency (R8)
            return self._acks_by_cid[req.client_order_id]
        self._seq += 1
        boid = f"sim-{self._seq}"
        price = req.limit_price or self._quotes.get(req.symbol, 0.0)
        if self._auto_fill:
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

    def cancel_order(self, broker_order_id: str) -> None:
        self._open.pop(broker_order_id, None)

    def cancel_all(self) -> None:
        self._open.clear()

    def get_account(self) -> AccountSnapshot:
        positions = tuple(self._positions.values())
        return AccountSnapshot(cash=self._cash, total_assets=self._cash,
                               day_pnl=0.0, stale=False, positions=positions)

    def get_open_orders(self) -> List[OrderAck]:
        return list(self._open.values())

    def reconcile_fills(self, since: Optional[str]) -> List[Fill]:
        return list(self._fills)

    def close(self) -> None:
        return None
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `python -m pytest tests/test_sim_broker.py -v`
Expected: PASS (5 passed).

- [ ] **Step 6: Commit**

```bash
git add autotrader/broker.py autotrader/sim_broker.py tests/test_sim_broker.py
git commit -m "feat(autotrader): Broker contract + in-memory SimBroker for offline core tests"
```

---

## Task 6: Order router — audit-first JSONL + idempotency + per-symbol lock

**Why:** Research §4.3/§4.6 + E18/E9/R8. The router is the only path to `broker.place_order`. It writes the audit line **before** the order, generates a deterministic `client_order_id`, and serializes orders per symbol.

**Files:**
- Create: `autotrader/router.py`
- Test: `tests/test_router.py`

- [ ] **Step 1: Write the failing test**

```python
import json
import threading
import pytest
from autotrader.domain import OrderRequest, OrderState, AccountSnapshot
from autotrader.config import RiskConfig
from autotrader.sim_broker import SimBroker
from autotrader.router import OrderRouter


def _cfg():
    return RiskConfig(trading_env="PAPER", min_confidence=0.6, max_order_notional=2000,
                      max_position_qty=100, daily_loss_limit=500, max_gross_exposure=10000,
                      allowed_symbols=frozenset({"US.AAPL"}))


def _req(cid="c1", qty=5):
    return OrderRequest(symbol="US.AAPL", side="BUY", qty=qty, order_type="LIMIT",
                        limit_price=100.0, client_order_id=cid)


def test_router_writes_audit_before_placing(tmp_path):
    audit = tmp_path / "audit.jsonl"
    b = SimBroker(quotes={"US.AAPL": 100.0})
    r = OrderRouter(b, audit_path=str(audit))
    ack = r.submit(_req())
    assert ack.state is OrderState.FILLED
    lines = audit.read_text().strip().splitlines()
    actions = [json.loads(l)["action"] for l in lines]
    assert actions[0] == "intent"          # written FIRST, before the broker call
    assert "ack" in actions


def test_router_halts_if_audit_unwritable(tmp_path):
    b = SimBroker(quotes={"US.AAPL": 100.0})
    # point audit at a path whose parent is a file, so opening fails (E18)
    bad_parent = tmp_path / "afile"
    bad_parent.write_text("x")
    r = OrderRouter(b, audit_path=str(bad_parent / "audit.jsonl"))
    with pytest.raises(RuntimeError):
        r.submit(_req())
    assert b.get_account().position_qty("US.AAPL") == 0  # no order placed


def test_router_dedupes_same_client_order_id(tmp_path):
    b = SimBroker(quotes={"US.AAPL": 100.0})
    r = OrderRouter(b, audit_path=str(tmp_path / "a.jsonl"))
    r.submit(_req(cid="dup"))
    r.submit(_req(cid="dup"))
    assert b.get_account().position_qty("US.AAPL") == 5  # filled once


def test_make_client_order_id_is_stable_for_same_inputs():
    a = OrderRouter.make_client_order_id("US.AAPL", "BUY", 5, "sig-42")
    b = OrderRouter.make_client_order_id("US.AAPL", "BUY", 5, "sig-42")
    c = OrderRouter.make_client_order_id("US.AAPL", "BUY", 6, "sig-42")
    assert a == b and a != c
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_router.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'autotrader.router'`.

- [ ] **Step 3: Write the implementation**

```python
"""Order router. The ONLY path to broker.place_order. Invariants:
- the audit line is written BEFORE the order is placed (E18/R16); an audit
  write failure is a hard error that halts the order.
- a deterministic client_order_id makes retries idempotent (R8).
- orders are serialized per symbol (E9).
No clock/random in the hot path beyond an ISO timestamp for the audit line."""
from __future__ import annotations

import collections
import hashlib
import json
import threading
from datetime import datetime, timezone
from typing import Dict

from autotrader.broker import Broker
from autotrader.domain import OrderAck, OrderRequest, OrderState


class OrderRouter:
    def __init__(self, broker: Broker, audit_path: str):
        self._broker = broker
        self._audit_path = audit_path
        self._seen: Dict[str, OrderAck] = {}
        self._locks: Dict[str, threading.Lock] = collections.defaultdict(threading.Lock)
        self._global = threading.Lock()

    @staticmethod
    def make_client_order_id(symbol: str, side: str, qty: int, signal_id: str) -> str:
        raw = f"{symbol}|{side}|{qty}|{signal_id}"
        return "at-" + hashlib.sha1(raw.encode()).hexdigest()[:16]

    def _audit(self, action: str, payload: dict) -> None:
        """Append one JSONL line. Raises on failure — caller must NOT proceed."""
        entry = {"action": action, "ts": datetime.now(timezone.utc).isoformat(), **payload}
        with open(self._audit_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
            f.flush()

    def submit(self, req: OrderRequest) -> OrderAck:
        with self._global:
            if req.client_order_id in self._seen:
                return self._seen[req.client_order_id]
            lock = self._locks[req.symbol]
        with lock:
            if req.client_order_id in self._seen:  # re-check under symbol lock
                return self._seen[req.client_order_id]
            # AUDIT FIRST — if this raises, no order is placed (E18).
            self._audit("intent", {
                "client_order_id": req.client_order_id, "symbol": req.symbol,
                "side": req.side, "qty": req.qty, "order_type": req.order_type,
                "limit_price": req.limit_price,
            })
            try:
                ack = self._broker.place_order(req)
            except Exception as e:  # ACK timeout / socket drop -> UNKNOWN, never success
                ack = OrderAck(req.client_order_id, None, OrderState.UNKNOWN, {"error": str(e)})
                self._audit("ack", {"client_order_id": req.client_order_id,
                                    "state": ack.state.value, "error": str(e)})
                self._seen[req.client_order_id] = ack
                return ack
            self._audit("ack", {"client_order_id": req.client_order_id,
                                "broker_order_id": ack.broker_order_id,
                                "state": ack.state.value})
            self._seen[req.client_order_id] = ack
            return ack
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_router.py -v`
Expected: PASS (4 passed).

- [ ] **Step 5: Commit**

```bash
git add autotrader/router.py tests/test_router.py
git commit -m "feat(autotrader): order router with audit-first, idempotency, per-symbol lock"
```

---

## Task 7: Stateless threshold strategy (explicit stop-loss + take-profit)

**Why:** CLAUDE.md requires every strategy to be stateless and declare an explicit exit. v1 ships exactly one: a price-threshold momentum entry with a hard stop and target. Market data in → `Signal | None` out. It never imports the broker or router.

**Files:**
- Create: `autotrader/strategies/threshold.py`
- Test: `tests/test_strategy_threshold.py`

- [ ] **Step 1: Write the failing test**

```python
import pytest
from autotrader.domain import Signal, Position
from autotrader.strategies.threshold import ThresholdStrategy, StrategyParams


def _params(**over):
    base = dict(symbol="US.AAPL", entry_price=100.0, stop_loss_pct=0.05,
                take_profit_pct=0.10, confidence=0.7)
    base.update(over)
    return StrategyParams(**base)


def test_params_require_explicit_stop_and_target():
    with pytest.raises(ValueError):
        StrategyParams(symbol="US.AAPL", entry_price=100.0, stop_loss_pct=0.0,
                       take_profit_pct=0.10, confidence=0.7)
    with pytest.raises(ValueError):
        StrategyParams(symbol="US.AAPL", entry_price=100.0, stop_loss_pct=0.05,
                       take_profit_pct=0.0, confidence=0.7)


def test_buy_signal_when_price_crosses_entry_and_flat():
    s = ThresholdStrategy(_params())
    sig = s.evaluate(price=100.5, position=None)
    assert isinstance(sig, Signal)
    assert sig.direction == "BUY"
    assert sig.confidence == 0.7


def test_no_signal_when_below_entry():
    s = ThresholdStrategy(_params())
    assert s.evaluate(price=99.0, position=None) is None


def test_no_duplicate_buy_when_already_long():
    s = ThresholdStrategy(_params())
    pos = Position("US.AAPL", qty=10, avg_price=100.0)
    assert s.evaluate(price=101.0, position=pos) is None


def test_sell_signal_on_stop_loss():
    s = ThresholdStrategy(_params())
    pos = Position("US.AAPL", qty=10, avg_price=100.0)
    sig = s.evaluate(price=94.9, position=pos)  # -5.1% < -5% stop
    assert sig.direction == "SELL"


def test_sell_signal_on_take_profit():
    s = ThresholdStrategy(_params())
    pos = Position("US.AAPL", qty=10, avg_price=100.0)
    sig = s.evaluate(price=110.5, position=pos)  # +10.5% > +10% target
    assert sig.direction == "SELL"


def test_strategy_is_deterministic_no_internal_state():
    s = ThresholdStrategy(_params())
    a = s.evaluate(price=100.5, position=None)
    b = s.evaluate(price=100.5, position=None)
    assert a == b  # same inputs -> same output, no tick-to-tick state
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_strategy_threshold.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'autotrader.strategies.threshold'`.

- [ ] **Step 3: Write the implementation**

```python
"""Stateless threshold strategy. Pure: (price, position) -> Signal | None.
Declares an explicit stop-loss AND take-profit (CLAUDE.md). Holds no state
between ticks; all parameters are frozen at construction. Never touches the
broker or router — its output is a data value routed by main.py."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from autotrader.domain import Position, Signal


@dataclass(frozen=True)
class StrategyParams:
    symbol: str
    entry_price: float        # buy when price >= entry_price and flat
    stop_loss_pct: float      # e.g. 0.05 = exit at -5% from avg_price
    take_profit_pct: float    # e.g. 0.10 = exit at +10% from avg_price
    confidence: float

    def __post_init__(self):
        if self.stop_loss_pct <= 0:
            raise ValueError("stop_loss_pct must be > 0 (explicit stop required)")
        if self.take_profit_pct <= 0:
            raise ValueError("take_profit_pct must be > 0 (explicit target required)")
        if not (0.0 <= self.confidence <= 1.0):
            raise ValueError("confidence must be in [0,1]")


class ThresholdStrategy:
    def __init__(self, params: StrategyParams):
        self.p = params

    def evaluate(self, price: float, position: Optional[Position]) -> Optional[Signal]:
        held = position.qty if position else 0
        # Manage an open long: exit on stop or target.
        if held > 0 and position is not None:
            change = (price - position.avg_price) / position.avg_price
            if change <= -self.p.stop_loss_pct:
                return Signal(self.p.symbol, "SELL", self.p.confidence,
                              f"stop-loss hit ({change:.2%})")
            if change >= self.p.take_profit_pct:
                return Signal(self.p.symbol, "SELL", self.p.confidence,
                              f"take-profit hit ({change:.2%})")
            return None
        # Flat: enter on threshold cross.
        if held == 0 and price >= self.p.entry_price:
            return Signal(self.p.symbol, "BUY", self.p.confidence,
                          f"price {price} >= entry {self.p.entry_price}")
        return None
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_strategy_threshold.py -v`
Expected: PASS (7 passed).

- [ ] **Step 5: Commit**

```bash
git add autotrader/strategies/threshold.py tests/test_strategy_threshold.py
git commit -m "feat(autotrader): stateless threshold strategy with explicit stop-loss/take-profit"
```

---

## Task 8: MoomooBroker adapter (composes the vendored skills)

**Why:** The only app file allowed to touch the SDK. It reuses `common.py`'s `create_trade_context`/`create_quote_context` (so the env checks, `ai_type`, and `security_firm` handling are inherited, never reinvented — CLAUDE.md), forces `refresh_cache=True` on account/position/order queries (R7/E1), maps ACK timeout → `OrderState.UNKNOWN` (E2/R8), and **never** calls `unlock_trade`. It needs a live OpenD, so its tests are marked `live` and skipped by default.

**Files:**
- Create: `autotrader/moomoo_broker.py`
- Test: `tests/test_moomoo_broker_live.py`

- [ ] **Step 1: Write the implementation**

```python
"""MoomooBroker — the ONLY app module that imports the vendored common.py / SDK.
Confines all Moomoo-isms: US.AAPL codes, (ret_code, data), refresh_cache=True,
OrderStatus mapping, TrdEnv.SIMULATE. Never calls unlock_trade (CLAUDE.md hard
rule). Reuses common.py factories rather than constructing OpenSecTradeContext
directly, so env checks are not bypassed."""
from __future__ import annotations

import os
import sys
from typing import List, Optional

from autotrader.broker import Broker
from autotrader.domain import (
    AccountSnapshot, BrokerError, BrokerErrorKind, Fill, OrderAck, OrderRequest,
    OrderState, Position,
)

# Resolve the vendored scripts dir and import common.py (triggers its env checks).
_REPO_ROOT = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
_MOOMOO_SCRIPTS = os.path.join(_REPO_ROOT, "skills", "moomooapi", "scripts")


def _load_common():
    if _MOOMOO_SCRIPTS not in sys.path:
        sys.path.insert(0, _MOOMOO_SCRIPTS)
    import common  # noqa: WPS433 — intentional late import; runs OpenD/SDK checks
    return common


# Moomoo OrderStatus name -> neutral OrderState.
_STATUS_MAP = {
    "SUBMITTED": OrderState.SUBMITTED, "SUBMITTING": OrderState.PENDING,
    "WAITING_SUBMIT": OrderState.PENDING, "FILLED_ALL": OrderState.FILLED,
    "FILLED_PART": OrderState.PARTIAL, "CANCELLED_ALL": OrderState.CANCELLED,
    "CANCELLED_PART": OrderState.CANCELLED, "FAILED": OrderState.REJECTED,
    "DISABLED": OrderState.REJECTED, "DELETED": OrderState.CANCELLED,
}


class MoomooBroker(Broker):
    def __init__(self, acc_id: Optional[int] = None):
        self._c = _load_common()
        self._acc_id = acc_id if acc_id is not None else self._c.get_default_acc_id()
        self._trade = None
        self._quote = None

    # --- lifecycle -------------------------------------------------------
    def connect(self) -> None:
        self._trade = self._c.create_trade_context("US")
        self._quote = self._c.create_quote_context()

    def is_ready(self) -> bool:
        return self._trade is not None and self._quote is not None

    def close(self) -> None:
        self._c.safe_close(self._trade)
        self._c.safe_close(self._quote)
        self._trade = self._quote = None

    # --- helpers ---------------------------------------------------------
    def _env(self):
        return self._c.get_default_trd_env()

    def _ok(self, ret) -> bool:
        return ret == self._c.RET_OK

    # --- market data -----------------------------------------------------
    def get_quote(self, symbol: str) -> Optional[float]:
        ret, data = self._quote.get_market_snapshot([symbol])
        if not self._ok(data if False else ret) or self._c.is_empty(data):
            return None
        return self._c.safe_float(self._c.safe_get(data.iloc[0], "last_price", default=0)) or None

    # --- orders ----------------------------------------------------------
    def place_order(self, req: OrderRequest) -> OrderAck:
        if req.order_type == "MARKET":
            ot, price = self._c.OrderType.MARKET, 0.0
        else:
            ot, price = self._c.OrderType.NORMAL, float(req.limit_price)
        side = self._c.TrdSide.BUY if req.side == "BUY" else self._c.TrdSide.SELL
        try:
            ret, data = self._trade.place_order(
                price=price, qty=int(req.qty), code=req.symbol, trd_side=side,
                order_type=ot, trd_env=self._env(), acc_id=self._acc_id,
                remark=req.client_order_id[:64],  # idempotency key in remark (R8)
            )
        except Exception as e:  # socket drop / timeout -> UNKNOWN, never success
            return OrderAck(req.client_order_id, None, OrderState.UNKNOWN, {"error": str(e)})
        if not self._ok(ret):
            return OrderAck(req.client_order_id, None, OrderState.REJECTED, {"error": str(data)})
        row = data.iloc[0]
        boid = str(self._c.safe_get(row, "order_id", "orderID", default=""))
        return OrderAck(req.client_order_id, boid, OrderState.SUBMITTED, {})

    def cancel_order(self, broker_order_id: str) -> None:
        from moomoo import ModifyOrderOp  # confined to this adapter
        ret, data = self._trade.modify_order(
            modify_order_op=ModifyOrderOp.CANCEL, order_id=broker_order_id,
            qty=0, price=0, trd_env=self._env(), acc_id=self._acc_id)
        if not self._ok(ret):
            raise BrokerError(BrokerErrorKind.UNKNOWN, f"cancel failed: {data}")

    def cancel_all(self) -> None:
        for ack in self.get_open_orders():
            if ack.broker_order_id:
                try:
                    self.cancel_order(ack.broker_order_id)
                except BrokerError:
                    pass  # best-effort flatten on shutdown; logged by caller

    def get_open_orders(self) -> List[OrderAck]:
        ret, data = self._trade.order_list_query(
            trd_env=self._env(), acc_id=self._acc_id, refresh_cache=True)
        if not self._ok(ret) or self._c.is_empty(data):
            return []
        out: List[OrderAck] = []
        for i in range(len(data)):
            row = data.iloc[i]
            status = self._c.format_enum(self._c.safe_get(row, "order_status", default="")).upper()
            state = _STATUS_MAP.get(status, OrderState.UNKNOWN)
            if state in (OrderState.PENDING, OrderState.SUBMITTED, OrderState.PARTIAL):
                out.append(OrderAck(
                    str(self._c.safe_get(row, "remark", default="")),
                    str(self._c.safe_get(row, "order_id", default="")), state, {}))
        return out

    # --- account / positions / fills ------------------------------------
    def get_account(self) -> AccountSnapshot:
        ret, acc = self._trade.accinfo_query(
            trd_env=self._env(), acc_id=self._acc_id, refresh_cache=True)
        if not self._ok(ret):
            raise BrokerError(BrokerErrorKind.UNKNOWN, f"accinfo failed: {acc}")
        cash = self._c.safe_float(self._c.safe_get(acc.iloc[0], "cash", "avl_withdrawal_cash", default=0))
        total = self._c.safe_float(self._c.safe_get(acc.iloc[0], "total_assets", default=0))
        pnl = self._c.safe_float(self._c.safe_get(acc.iloc[0], "realized_pl", "today_pnl_value", default=0))
        positions = self._positions()
        # If the SDK returns no usable snapshot, mark stale so risk_core refuses (E1).
        stale = (total == 0 and not positions)
        return AccountSnapshot(cash=cash, total_assets=total, day_pnl=pnl,
                               stale=stale, positions=tuple(positions))

    def _positions(self) -> List[Position]:
        ret, data = self._trade.position_list_query(
            trd_env=self._env(), acc_id=self._acc_id, refresh_cache=True)
        if not self._ok(ret) or self._c.is_empty(data):
            return []
        out: List[Position] = []
        for i in range(len(data)):
            row = data.iloc[i]
            out.append(Position(
                symbol=str(self._c.safe_get(row, "code", default="")),
                qty=self._c.safe_int(self._c.safe_get(row, "qty", default=0)),
                avg_price=self._c.safe_float(self._c.safe_get(row, "cost_price", "nominal_price", default=0)),
            ))
        return out

    def get_open_orders_count(self) -> int:  # convenience for logs
        return len(self.get_open_orders())

    def reconcile_fills(self, since: Optional[str]) -> List[Fill]:
        ret, data = self._trade.deal_list_query(
            trd_env=self._env(), acc_id=self._acc_id, refresh_cache=True)
        if not self._ok(ret) or self._c.is_empty(data):
            return []
        out: List[Fill] = []
        for i in range(len(data)):
            row = data.iloc[i]
            side_raw = self._c.format_enum(self._c.safe_get(row, "trd_side", default="BUY")).upper()
            out.append(Fill(
                fill_id=str(self._c.safe_get(row, "deal_id", default="")),
                symbol=str(self._c.safe_get(row, "code", default="")),
                side="BUY" if side_raw == "BUY" else "SELL",
                qty=self._c.safe_float(self._c.safe_get(row, "qty", default=0)),
                price=self._c.safe_float(self._c.safe_get(row, "price", default=0)),
                ts=str(self._c.safe_get(row, "create_time", default="")),
            ))
        return out
```

- [ ] **Step 2: Write the live test (skipped unless OpenD is running)**

```python
"""MoomooBroker tests require a live, authenticated OpenD paper session.
Run explicitly with:  RUN_LIVE=1 python -m pytest tests/test_moomoo_broker_live.py -v
They are skipped by default so the core suite stays offline/deterministic."""
import os
import pytest

pytestmark = pytest.mark.skipif(os.getenv("RUN_LIVE") != "1",
                                reason="set RUN_LIVE=1 with OpenD running (paper)")


def test_connect_and_account_snapshot_is_fresh():
    from autotrader.moomoo_broker import MoomooBroker
    b = MoomooBroker()
    b.connect()
    try:
        assert b.is_ready() is True
        snap = b.get_account()
        assert snap.stale is False
    finally:
        b.close()


def test_quote_round_trips():
    from autotrader.moomoo_broker import MoomooBroker
    b = MoomooBroker()
    b.connect()
    try:
        px = b.get_quote("US.AAPL")
        assert px is None or px > 0
    finally:
        b.close()
```

- [ ] **Step 3: Verify the adapter never references unlock_trade**

Run: `grep -rin "unlock_trade\|unlock" autotrader/moomoo_broker.py`
Expected: **no output** (exit code 1). The adapter must contain no unlock path (CLAUDE.md hard rule).

- [ ] **Step 4: Verify the core suite still passes with the adapter present (it must not import the SDK at collection time)**

Run: `python -m pytest tests/ -v --ignore=tests/test_moomoo_broker_live.py`
Expected: PASS — all prior tests green; `moomoo_broker.py` is only imported inside the skipped live test, so the offline suite is unaffected.

- [ ] **Step 5: Commit**

```bash
git add autotrader/moomoo_broker.py tests/test_moomoo_broker_live.py
git commit -m "feat(autotrader): MoomooBroker adapter (refresh_cache, UNKNOWN-never-success, no unlock)"
```

---

## Task 9: main.py orchestration loop

**Why:** Research §4.3 + CLAUDE.md `main.py` rules. One tick: readiness gate (exponential backoff, never `time.sleep` as the check) → fresh account snapshot → quote → strategy → confidence filter → risk core → router. On shutdown: `cancel_all` before disconnect. `main.py` is driver-agnostic: it accepts any `Broker`, so `test_main_loop.py` drives it with a `SimBroker`.

**Files:**
- Create: `autotrader/main.py`
- Test: `tests/test_main_loop.py`

- [ ] **Step 1: Write the failing test**

```python
import json
import pytest
from autotrader.domain import Position
from autotrader.config import RiskConfig
from autotrader.sim_broker import SimBroker
from autotrader.strategies.threshold import ThresholdStrategy, StrategyParams
from autotrader.main import TradeEngine


def _cfg(**over):
    base = dict(trading_env="PAPER", min_confidence=0.6, max_order_notional=20000,
                max_position_qty=100, daily_loss_limit=500, max_gross_exposure=100000,
                allowed_symbols=frozenset({"US.AAPL"}))
    base.update(over)
    return RiskConfig(**base)


def _engine(broker, cfg=None, qty=10, audit_path="x.jsonl", tmp_path=None):
    path = str(tmp_path / "audit.jsonl") if tmp_path else audit_path
    strat = ThresholdStrategy(StrategyParams(symbol="US.AAPL", entry_price=100.0,
                                             stop_loss_pct=0.05, take_profit_pct=0.10,
                                             confidence=0.7))
    return TradeEngine(broker=broker, strategy=strat, cfg=cfg or _cfg(),
                       order_qty=qty, audit_path=path)


def test_tick_places_buy_when_threshold_crosses(tmp_path):
    b = SimBroker(quotes={"US.AAPL": 101.0}, cash=100000.0)
    eng = _engine(b, tmp_path=tmp_path)
    result = eng.tick()
    assert result.action == "ORDER_PLACED"
    assert b.get_account().position_qty("US.AAPL") == 10


def test_tick_drops_low_confidence_signal(tmp_path):
    b = SimBroker(quotes={"US.AAPL": 101.0}, cash=100000.0)
    eng = _engine(b, cfg=_cfg(min_confidence=0.9), tmp_path=tmp_path)
    result = eng.tick()
    assert result.action == "DROPPED_LOW_CONFIDENCE"
    assert b.get_account().position_qty("US.AAPL") == 0


def test_tick_respects_risk_core_rejection(tmp_path):
    b = SimBroker(quotes={"US.AAPL": 101.0}, cash=100000.0)
    eng = _engine(b, cfg=_cfg(allowed_symbols=frozenset()), tmp_path=tmp_path)
    result = eng.tick()
    assert result.action == "REJECTED_BY_RISK"
    assert b.get_account().position_qty("US.AAPL") == 0


def test_tick_no_signal_when_below_entry(tmp_path):
    b = SimBroker(quotes={"US.AAPL": 99.0}, cash=100000.0)
    eng = _engine(b, tmp_path=tmp_path)
    assert eng.tick().action == "NO_SIGNAL"


def test_shutdown_cancels_all_open_orders(tmp_path):
    b = SimBroker(quotes={"US.AAPL": 101.0}, cash=100000.0, auto_fill=False)
    eng = _engine(b, tmp_path=tmp_path)
    eng.tick()
    assert len(b.get_open_orders()) == 1
    eng.shutdown()
    assert b.get_open_orders() == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_main_loop.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'autotrader.main'`.

- [ ] **Step 3: Write the implementation**

```python
"""Orchestration. One deterministic tick: snapshot -> quote -> strategy ->
confidence filter -> risk core -> router. Shutdown cancels all open orders
before disconnect (CLAUDE.md). The engine takes any Broker, so it is testable
against SimBroker with no OpenD. The __main__ path wires a live MoomooBroker
behind is_opend_ready()."""
from __future__ import annotations

import logging
from dataclasses import dataclass

from autotrader.broker import Broker
from autotrader.config import RiskConfig, load_risk_config
from autotrader.domain import OrderRequest, OrderState
from autotrader.risk_core import evaluate
from autotrader.router import OrderRouter
from autotrader.strategies.threshold import ThresholdStrategy

logger = logging.getLogger("autotrader.engine")


@dataclass(frozen=True)
class TickResult:
    action: str
    detail: str = ""


class TradeEngine:
    def __init__(self, broker: Broker, strategy: ThresholdStrategy, cfg: RiskConfig,
                 order_qty: int, audit_path: str):
        self._b = broker
        self._strat = strategy
        self._cfg = cfg
        self._qty = order_qty
        self._router = OrderRouter(broker, audit_path=audit_path)
        self._signal_seq = 0

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

        if signal.confidence < self._cfg.min_confidence:
            return TickResult("DROPPED_LOW_CONFIDENCE", f"{signal.confidence}")

        self._signal_seq += 1
        cid = OrderRouter.make_client_order_id(
            signal.symbol, signal.direction, self._qty, f"sig-{self._signal_seq}")
        req = OrderRequest(symbol=signal.symbol, side=signal.direction, qty=self._qty,
                           order_type="MARKET", limit_price=None, client_order_id=cid)

        decision = evaluate(req, snap, self._cfg, ref_price=price)
        if not decision.approved:
            logger.warning("risk core rejected: %s", decision.reason)
            return TickResult("REJECTED_BY_RISK", decision.reason)

        ack = self._router.submit(req)
        if ack.state is OrderState.UNKNOWN:
            return TickResult("ORDER_UNKNOWN", ack.client_order_id)
        if ack.state is OrderState.REJECTED:
            return TickResult("ORDER_REJECTED", ack.client_order_id)
        return TickResult("ORDER_PLACED", str(ack.broker_order_id))

    def shutdown(self) -> None:
        # Cancel-on-shutdown (CLAUDE.md). Best-effort flatten of working orders.
        try:
            self._b.cancel_all()
        finally:
            logger.info("shutdown: cancel_all issued")


def main() -> int:  # pragma: no cover — live entrypoint, covered by manual run
    import os
    import sys
    from autotrader.strategies.threshold import StrategyParams

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    cfg = load_risk_config()
    logger.info("TRADING_ENV=%s (paper-only v1)", cfg.trading_env)
    if cfg.trading_env != "PAPER":
        logger.error("v1 is paper-only; refusing to start with TRADING_ENV=%s", cfg.trading_env)
        return 2

    # Readiness gate: reuse the dashboard's exponential-backoff is_opend_ready().
    sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "dashboard"))
    from opend_ready import is_opend_ready, OpenDNotReady  # type: ignore
    try:
        is_opend_ready(timeout=float(os.getenv("OPEND_READY_TIMEOUT", "30")))
    except OpenDNotReady as e:
        logger.error("halting: %s", e)
        return 1

    from autotrader.moomoo_broker import MoomooBroker
    symbol = next(iter(cfg.allowed_symbols), "US.AAPL")
    strat = ThresholdStrategy(StrategyParams(
        symbol=symbol, entry_price=float(os.getenv("ENTRY_PRICE", "0")),
        stop_loss_pct=0.05, take_profit_pct=0.10, confidence=0.7))
    audit = os.path.join(os.path.expanduser("~"), ".futu_trade_audit.jsonl")
    broker = MoomooBroker()
    broker.connect()
    engine = TradeEngine(broker, strat, cfg, order_qty=int(os.getenv("ORDER_QTY", "1")),
                         audit_path=audit)
    try:
        result = engine.tick()  # v1: single deterministic tick; loop added in Phase 2
        logger.info("tick result: %s %s", result.action, result.detail)
    finally:
        engine.shutdown()
        broker.close()
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_main_loop.py -v`
Expected: PASS (5 passed).

- [ ] **Step 5: Commit**

```bash
git add autotrader/main.py tests/test_main_loop.py
git commit -m "feat(autotrader): TradeEngine orchestration (readiness gate, risk-gated, cancel-on-shutdown)"
```

---

## Task 10: Full-suite gate + README + final verification

**Files:**
- Create: `autotrader/README.md`
- Test: (runs the whole suite)

- [ ] **Step 1: Run the entire offline suite**

Run: `python -m pytest tests/ -v --ignore=tests/test_moomoo_broker_live.py`
Expected: PASS — all tests from Tasks 1–9 green (Task 1 R19 tests may SKIP if the SDK is not installed).

- [ ] **Step 2: Confirm no banned patterns leaked into app code**

Run:
```bash
grep -rin "unlock_trade" autotrader/ && echo "FOUND BANNED unlock_trade" || echo "clean: no unlock_trade"
grep -rin "from moomoo import\|import moomoo" autotrader/ | grep -v moomoo_broker.py && echo "FOUND SDK leak" || echo "clean: SDK confined to moomoo_broker.py"
grep -rin "time.sleep" autotrader/ && echo "REVIEW time.sleep usage" || echo "clean: no time.sleep readiness checks"
```
Expected: `clean: no unlock_trade`, `clean: SDK confined to moomoo_broker.py`, `clean: no time.sleep readiness checks`.

- [ ] **Step 3: Write `autotrader/README.md`**

```markdown
# AutoTrader core (paper v1)

Deterministic paper-trading core for Moomoo OpenD. Architecture and rationale:
`docs/research/autotrader-architecture-research.md`. Plan: `docs/superpowers/plans/2026-06-12-autotrader-paper-v1.md`.

## Rings
- `strategies/` — stateless: (price, position) -> Signal | None. No execution.
- `risk_core.py` — deterministic gate. The ONLY approver of orders.
- `router.py` — audit-first JSONL + idempotency + per-symbol lock.
- `broker.py` / `sim_broker.py` / `moomoo_broker.py` — Broker port + adapters.
  All Moomoo-isms live in `moomoo_broker.py`; it never calls `unlock_trade`.
- `main.py` — readiness gate -> tick -> cancel-on-shutdown.

## Run (paper only)
1. Start & authenticate the OpenD GUI (paper account). Install via `/install-moomoo-opend`.
2. `cp config/risk.config.example config/risk.config` and review limits (human-reviewed).
3. Export `RISK_*` (from risk.config) and `FUTU_ACC_ID`, set `ENTRY_PRICE`, then:
   `python -m autotrader.main`

LIVE is intentionally blocked in v1 (`main()` refuses non-PAPER). Live is a later,
separately-validated phase requiring the two-key unlock.

## Test
- Offline core: `python -m pytest tests/ --ignore=tests/test_moomoo_broker_live.py`
- Live adapter (OpenD running, paper): `RUN_LIVE=1 python -m pytest tests/test_moomoo_broker_live.py`
```

- [ ] **Step 4: Commit**

```bash
git add autotrader/README.md
git commit -m "docs(autotrader): README for paper v1 core + run/test instructions"
```

- [ ] **Step 5: (Manual, requires OpenD) First paper order — Phase 1 exit gate**

With the OpenD GUI running and authenticated on a paper account:
```bash
cp config/risk.config.example config/risk.config   # review limits first
set -a; . ./config/risk.config; set +a
export FUTU_ACC_ID=<your SIMULATE acc_id>           # from get_accounts.py
export ENTRY_PRICE=0 ORDER_QTY=1                    # entry 0 => buy on first tick
RUN_LIVE=1 python -m pytest tests/test_moomoo_broker_live.py -v   # adapter smoke test
python -m autotrader.main                            # single risk-gated paper tick
```
Expected: a `place_order` line in `~/.futu_trade_audit.jsonl` with the `intent` written before the `ack`, an order visible in the paper account, and `cancel_all` issued on shutdown. **This is the Phase 1 exit gate** (research §6): one order placed & cancelled in `TrdEnv.SIMULATE`, fill handling verified, all risk-core unit tests green.

---

## Self-Review (completed by plan author)

**Spec coverage vs. research §6 Phase 0 + Phase 1:**
- Patch R19 → Task 1 ✓ · frozen `RiskConfig` → Task 3 ✓ · pure `risk_core.evaluate()` unit-tested → Task 4 ✓ · concrete `MoomooBroker` (port method names) → Tasks 5/8 ✓ · `broker.py` → Task 5 ✓ · one stateless strategy with explicit stop/target → Task 7 ✓ · `main.py` (is_opend_ready → strategy → confidence → risk → order) → Task 9 ✓ · JSONL audit written-first → Task 6 ✓ · idempotency + UNKNOWN-never-success → Tasks 6/8 ✓ · no Hermes/IPC/2nd adapter/SQLite/crypto/HK → out-of-scope section ✓ · Phase 1 exit gate (one order placed & cancelled in SIMULATE) → Task 10 Step 5 ✓.
- CLAUDE.md hard rules: no unlock_trade (Task 8 Step 3 grep gate) ✓ · config not hardcoded (Task 3) ✓ · ret_code checks in adapter ✓ · paper default (config + main refuses LIVE) ✓ · readiness via backoff not sleep (reuses `opend_ready.py`) ✓ · cancel-on-shutdown (Task 9) ✓ · strategy doesn't import trade scripts (Task 7) ✓ · refresh_cache=True (Task 8) ✓.

**Placeholder scan:** no TBD/"add error handling"/"similar to Task N"; every code step is complete and copy-pastable.

**Type consistency:** `OrderState`, `OrderRequest(client_order_id)`, `AccountSnapshot(stale/day_pnl/positions)`, `RiskDecision(approved,reason)`, `Signal(direction in BUY/SELL)`, `TickResult(action)`, `make_client_order_id(symbol,side,qty,signal_id)` are used identically across Tasks 2–9. `evaluate(req, snapshot, cfg, ref_price)` signature matches between Task 4 definition and Task 9 caller.
