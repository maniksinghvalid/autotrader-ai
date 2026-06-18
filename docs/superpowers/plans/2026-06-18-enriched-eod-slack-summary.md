# Enriched EOD Slack Summary Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the flat fill-dump EOD Slack summary with a strategy-grouped report (thesis + economics) and a meaningful P&L header (realized/unrealized split + capital-flow line), all from the DB projection.

**Architecture:** Four new pure, independently-tested helpers under `autotrader/reporting/` (option-code parser, strategy classifier, per-strategy economics, capital-flow) supply structure the reporter composes. The reporter stays SDK/broker-free. Unrealized P&L is the only write-path change: it's threaded from the account snapshot into the `performance` table so the reporter can read it. Grouping is parse-only (decode the option code, group fills by underlying) — no `correlation_id` persistence.

**Tech Stack:** Python 3, pytest, stdlib `re`/`datetime`/`dataclasses`, SQLite (`autotrader.db.DB`), Slack Block Kit JSON.

## Global Constraints

- The reporter is SDK-free and broker-free: it reads ONLY the SQLite projection (`autotrader.db.DB`) and POSTs over stdlib urllib. It has no path to place/cancel orders. (eod_reporter.py module contract)
- `send_eod_report` must NEVER raise into the trading loop; every new helper is total (returns `None`/generic on bad input, never raises). (existing guarantee)
- Grouping is **parse-only**: decode the option code and group by underlying. No `correlation_id`/overlay-type persistence on trades.
- Strategy label comes from **structural classification of the parsed legs** (authoritative); the signal `rationale` supplies the thesis TEXT only.
- Option code format (US equity): `US.{UNDER}{YYMMDD}{C|P}{strike*1000}`; strike = trailing digits ÷ 1000; multiplier 100.
- No live-price lookups: moneyness basis = today's stock-leg avg fill, else `positions.avg_price`, else `None` (omit the figure).
- New write-path params are **defaulted** (`unrealized_pnl: float = 0.0`) so existing constructors/callers/tests keep working.
- TDD: failing test first, watch it fail, minimal implementation, watch it pass, run the full suite, commit.
- Run the suite with: `PYTHONPATH=. python3 -m pytest tests/ -q`

---

### Task 1: Option-code parser

**Files:**
- Create: `autotrader/reporting/option_code.py`
- Test: `tests/test_reporting_option_code.py`

**Interfaces:**
- Produces:
  - `ParsedOption` (frozen dataclass): `underlying: str`, `expiry: datetime.date`, `strike: float`, `right: str` (`"CALL"`|`"PUT"`), `multiplier: int = 100`.
  - `parse_option_code(code: str) -> Optional[ParsedOption]` — returns `None` for a plain stock symbol or any non-matching/malformed code; never raises.

- [ ] **Step 1: Write the failing test**

Create `tests/test_reporting_option_code.py`:

```python
from datetime import date

from autotrader.reporting.option_code import ParsedOption, parse_option_code


def test_parses_call_code():
    p = parse_option_code("US.CLOV260821C7000")
    assert p == ParsedOption(underlying="US.CLOV", expiry=date(2026, 8, 21),
                             strike=7.00, right="CALL", multiplier=100)


def test_parses_put_code():
    p = parse_option_code("US.SPCE260821P3000")
    assert p.underlying == "US.SPCE" and p.right == "PUT"
    assert p.strike == 3.00 and p.expiry == date(2026, 8, 21)


def test_parses_fractional_strike():
    p = parse_option_code("US.DIVO260821C47410")
    assert p.strike == 47.41 and p.right == "CALL"


def test_plain_stock_symbol_returns_none():
    assert parse_option_code("US.SCHF") is None


def test_malformed_code_returns_none():
    assert parse_option_code("") is None
    assert parse_option_code("US.AAPL26XXC1000") is None
    assert parse_option_code("US.AAPL260821X1000") is None  # bad right letter
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH=. python3 -m pytest tests/test_reporting_option_code.py -q`
Expected: FAIL — `ModuleNotFoundError: autotrader.reporting.option_code`.

- [ ] **Step 3: Write minimal implementation**

Create `autotrader/reporting/option_code.py`:

```python
"""Pure decoder for US equity option codes: US.{UNDER}{YYMMDD}{C|P}{strike*1000}.
Total — returns None for stock symbols or malformed codes, never raises. SDK-free."""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime
from typing import Optional

# Non-greedy underlying, then a 6-digit date, a C/P, and the strike*1000 integer.
_CODE_RE = re.compile(r"^(?P<under>.+?)(?P<ymd>\d{6})(?P<cp>[CP])(?P<strike>\d+)$")


@dataclass(frozen=True)
class ParsedOption:
    underlying: str
    expiry: date
    strike: float
    right: str          # "CALL" | "PUT"
    multiplier: int = 100


def parse_option_code(code: str) -> Optional[ParsedOption]:
    if not code:
        return None
    m = _CODE_RE.match(code)
    if not m:
        return None
    try:
        expiry = datetime.strptime(m.group("ymd"), "%y%m%d").date()
    except ValueError:
        return None
    right = "CALL" if m.group("cp") == "C" else "PUT"
    strike = int(m.group("strike")) / 1000.0
    return ParsedOption(underlying=m.group("under"), expiry=expiry,
                        strike=strike, right=right, multiplier=100)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `PYTHONPATH=. python3 -m pytest tests/test_reporting_option_code.py -q`
Expected: PASS (5 passed).

- [ ] **Step 5: Run the full suite + commit**

Run: `PYTHONPATH=. python3 -m pytest tests/ -q` (expect all pass), then:

```bash
git add autotrader/reporting/option_code.py tests/test_reporting_option_code.py
git commit -m "feat(reporting): pure US option-code parser

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 2: Strategy classifier

**Files:**
- Create: `autotrader/reporting/classify.py`
- Test: `tests/test_reporting_classify.py`

**Interfaces:**
- Consumes: `ParsedOption` (Task 1).
- Produces:
  - `Leg` (frozen dataclass): `side: str` (`"BUY"`|`"SELL"`), `qty: float`, `price: float`, `option: Optional[ParsedOption]` (`None` = stock leg).
  - `classify_strategy(legs: Sequence[Leg]) -> str` — structural label; `"Strategy"` for anything unrecognized; never raises.

- [ ] **Step 1: Write the failing test**

Create `tests/test_reporting_classify.py`:

```python
from datetime import date

from autotrader.reporting.classify import Leg, classify_strategy
from autotrader.reporting.option_code import ParsedOption


def _call(strike, side="SELL"):
    return Leg(side, 2, 0.20, ParsedOption("US.X", date(2026, 8, 21), strike, "CALL"))


def _put(strike, side="BUY"):
    return Leg(side, 2, 0.16, ParsedOption("US.X", date(2026, 7, 31), strike, "PUT"))


def _stock(side="BUY"):
    return Leg(side, 200, 5.0, None)


def test_covered_call():
    assert classify_strategy([_stock(), _call(7.0)]) == "Covered Call"


def test_protective_put():
    assert classify_strategy([_stock(), _put(4.5)]) == "Protective Put"


def test_collar():
    assert classify_strategy([_stock(), _call(6.0), _put(4.5)]) == "Collar"


def test_bear_put_spread():
    assert classify_strategy([_put(5.0, "BUY"), _put(4.5, "SELL")]) == "Bear Put Spread"


def test_pmcc_two_calls():
    assert classify_strategy([_call(5.0, "BUY"), _call(7.0, "SELL")]) == "PMCC / Call Diagonal"


def test_leap_single_long_call():
    assert classify_strategy([_call(5.0, "BUY")]) == "LEAP"


def test_covered_call_existing_shares():
    assert classify_strategy([_call(47.41, "SELL")]) == "Covered Call (existing shares)"


def test_stock_entry_and_exit():
    assert classify_strategy([_stock("BUY")]) == "Stock entry"
    assert classify_strategy([_stock("SELL")]) == "Stock exit"


def test_unrecognized_is_generic():
    assert classify_strategy([]) == "Strategy"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH=. python3 -m pytest tests/test_reporting_classify.py -q`
Expected: FAIL — `ModuleNotFoundError: autotrader.reporting.classify`.

- [ ] **Step 3: Write minimal implementation**

Create `autotrader/reporting/classify.py`:

```python
"""Pure structural classifier: label a single underlying's day of legs as a
known options strategy. Authoritative (doesn't read signal text). Never raises."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Sequence

from autotrader.reporting.option_code import ParsedOption


@dataclass(frozen=True)
class Leg:
    side: str                       # "BUY" | "SELL"
    qty: float
    price: float
    option: Optional[ParsedOption]  # None = stock leg


def classify_strategy(legs: Sequence[Leg]) -> str:
    stock = [l for l in legs if l.option is None]
    calls = [l for l in legs if l.option is not None and l.option.right == "CALL"]
    puts = [l for l in legs if l.option is not None and l.option.right == "PUT"]
    has_stock = bool(stock)
    short_call = any(c.side == "SELL" for c in calls)
    long_call = any(c.side == "BUY" for c in calls)
    long_put = any(p.side == "BUY" for p in puts)
    short_put = any(p.side == "SELL" for p in puts)

    if has_stock and short_call and long_put:
        return "Collar"
    if has_stock and short_call and not puts:
        return "Covered Call"
    if has_stock and long_put and not calls:
        return "Protective Put"
    if not has_stock and long_put and short_put:
        return "Bear Put Spread"
    if not has_stock and not puts:
        if len(calls) >= 2:
            return "PMCC / Call Diagonal"
        if len(calls) == 1 and long_call:
            return "LEAP"
        if len(calls) == 1 and short_call:
            return "Covered Call (existing shares)"
    if has_stock and not calls and not puts:
        return "Stock entry" if stock[0].side == "BUY" else "Stock exit"
    return "Strategy"
```

- [ ] **Step 4: Run test to verify it passes**

Run: `PYTHONPATH=. python3 -m pytest tests/test_reporting_classify.py -q`
Expected: PASS (9 passed).

- [ ] **Step 5: Run the full suite + commit**

Run: `PYTHONPATH=. python3 -m pytest tests/ -q` (expect all pass), then:

```bash
git add autotrader/reporting/classify.py tests/test_reporting_classify.py
git commit -m "feat(reporting): structural strategy classifier

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 3: Per-strategy economics

**Files:**
- Create: `autotrader/reporting/economics.py`
- Test: `tests/test_reporting_economics.py`

**Interfaces:**
- Consumes: `Leg` (Task 2), `ParsedOption` (Task 1).
- Produces:
  - `StrategyEconomics` (frozen dataclass): `net_premium: Optional[float]`, `floor: Optional[float]`, `cap: Optional[float]`, `floor_pct: Optional[float]`, `cap_pct: Optional[float]`, `hedge_cost_pct: Optional[float]`.
  - `compute_economics(legs: Sequence[Leg], basis: Optional[float]) -> StrategyEconomics`. `net_premium` is over OPTION legs only (SELL +, BUY −, ×qty×multiplier). `floor` = lowest long-put strike; `cap` = lowest short-call strike. `*_pct` are vs `basis` (`None` when `basis` is `None`). `hedge_cost_pct` = `long_put.price / basis * 100` (`None` if no long put or no basis). Never raises.

- [ ] **Step 1: Write the failing test**

Create `tests/test_reporting_economics.py`:

```python
from datetime import date

from autotrader.reporting.classify import Leg
from autotrader.reporting.economics import StrategyEconomics, compute_economics
from autotrader.reporting.option_code import ParsedOption


def _opt(strike, right):
    return ParsedOption("US.X", date(2026, 8, 21), strike, right)


def test_covered_call_credit_and_cap():
    legs = [Leg("BUY", 200, 4.99, None),
            Leg("SELL", 2, 0.20, _opt(7.0, "CALL"))]
    e = compute_economics(legs, basis=4.99)
    assert abs(e.net_premium - 40.0) < 1e-6        # +2*0.20*100
    assert e.cap == 7.0
    assert abs(e.cap_pct - ((7.0 - 4.99) / 4.99 * 100)) < 1e-6
    assert e.floor is None and e.hedge_cost_pct is None


def test_protective_put_debit_floor_hedgecost():
    legs = [Leg("BUY", 100, 3.33, None),
            Leg("BUY", 1, 0.48, _opt(3.0, "PUT"))]
    e = compute_economics(legs, basis=3.33)
    assert abs(e.net_premium - (-48.0)) < 1e-6     # -1*0.48*100
    assert e.floor == 3.0
    assert abs(e.floor_pct - ((3.0 - 3.33) / 3.33 * 100)) < 1e-6
    assert abs(e.hedge_cost_pct - (0.48 / 3.33 * 100)) < 1e-6


def test_collar_net_and_band():
    legs = [Leg("BUY", 200, 5.03, None),
            Leg("SELL", 2, 0.14, _opt(6.0, "CALL")),
            Leg("BUY", 2, 0.16, _opt(4.5, "PUT"))]
    e = compute_economics(legs, basis=5.03)
    assert abs(e.net_premium - (-4.0)) < 1e-6      # +28 -32
    assert e.floor == 4.5 and e.cap == 6.0


def test_missing_basis_omits_pcts():
    legs = [Leg("SELL", 2, 0.20, _opt(7.0, "CALL"))]
    e = compute_economics(legs, basis=None)
    assert e.cap == 7.0
    assert e.cap_pct is None and e.floor_pct is None and e.hedge_cost_pct is None
    assert abs(e.net_premium - 40.0) < 1e-6
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH=. python3 -m pytest tests/test_reporting_economics.py -q`
Expected: FAIL — `ModuleNotFoundError: autotrader.reporting.economics`.

- [ ] **Step 3: Write minimal implementation**

Create `autotrader/reporting/economics.py`:

```python
"""Pure per-strategy economics from a group's parsed legs + an optional basis
price. Every figure is Optional; missing inputs yield None. Never raises."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Sequence

from autotrader.reporting.classify import Leg


@dataclass(frozen=True)
class StrategyEconomics:
    net_premium: Optional[float]    # signed $ over option legs (SELL +, BUY -)
    floor: Optional[float]          # lowest long-put strike
    cap: Optional[float]            # lowest short-call strike
    floor_pct: Optional[float]      # (floor-basis)/basis*100
    cap_pct: Optional[float]        # (cap-basis)/basis*100
    hedge_cost_pct: Optional[float] # long-put premium / basis * 100


def _pct(strike: Optional[float], basis: Optional[float]) -> Optional[float]:
    if strike is None or not basis:
        return None
    return (strike - basis) / basis * 100.0


def compute_economics(legs: Sequence[Leg], basis: Optional[float]) -> StrategyEconomics:
    option_legs = [l for l in legs if l.option is not None]
    net_premium = None
    if option_legs:
        net_premium = sum(
            (1.0 if l.side == "SELL" else -1.0) * l.qty * l.price * l.option.multiplier
            for l in option_legs)
    long_puts = [l for l in option_legs if l.option.right == "PUT" and l.side == "BUY"]
    short_calls = [l for l in option_legs if l.option.right == "CALL" and l.side == "SELL"]
    floor = min((l.option.strike for l in long_puts), default=None)
    cap = min((l.option.strike for l in short_calls), default=None)
    hedge_cost_pct = None
    if long_puts and basis:
        hedge_cost_pct = long_puts[0].price / basis * 100.0
    return StrategyEconomics(
        net_premium=net_premium, floor=floor, cap=cap,
        floor_pct=_pct(floor, basis), cap_pct=_pct(cap, basis),
        hedge_cost_pct=hedge_cost_pct)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `PYTHONPATH=. python3 -m pytest tests/test_reporting_economics.py -q`
Expected: PASS (4 passed).

- [ ] **Step 5: Run the full suite + commit**

Run: `PYTHONPATH=. python3 -m pytest tests/ -q` (expect all pass), then:

```bash
git add autotrader/reporting/economics.py tests/test_reporting_economics.py
git commit -m "feat(reporting): per-strategy economics (net premium, floor/cap, hedge cost)

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 4: Capital-flow header figures

**Files:**
- Create: `autotrader/reporting/capital_flow.py`
- Test: `tests/test_reporting_capital_flow.py`

**Interfaces:**
- Consumes: `parse_option_code` (Task 1).
- Produces:
  - `CapitalFlow` (frozen dataclass): `premium_collected: float`, `premium_paid: float`, `net_cash_deployed: float`.
  - `compute_capital_flow(fills: Iterable[Tuple[str, str, float, float]]) -> CapitalFlow` where each fill is `(symbol, side, qty, price)`. Option legs (symbol parses) use ×100; stock legs ×1. `premium_collected` = Σ SELL option premium (positive), `premium_paid` = Σ BUY option premium (positive), `net_cash_deployed` = Σ over ALL legs of `(+qty*price*mult if SELL else -qty*price*mult)`. Never raises.

- [ ] **Step 1: Write the failing test**

Create `tests/test_reporting_capital_flow.py`:

```python
from autotrader.reporting.capital_flow import CapitalFlow, compute_capital_flow


def test_mixed_day_collected_paid_and_net():
    fills = [
        ("US.CLOV", "BUY", 200, 4.99),                 # stock buy  -> -998
        ("US.CLOV260821C7000", "SELL", 2, 0.20),       # call credit -> +40
        ("US.SPCE", "BUY", 100, 3.33),                 # stock buy  -> -333
        ("US.SPCE260821P3000", "BUY", 1, 0.48),        # put debit  -> -48
        ("US.SCHF", "SELL", 100, 28.27),               # stock sell -> +2827
    ]
    cf = compute_capital_flow(fills)
    assert abs(cf.premium_collected - 40.0) < 1e-6
    assert abs(cf.premium_paid - 48.0) < 1e-6
    assert abs(cf.net_cash_deployed - (-998 + 40 - 333 - 48 + 2827)) < 1e-6


def test_no_options_is_zero_premium():
    cf = compute_capital_flow([("US.SCHF", "SELL", 100, 28.27)])
    assert cf.premium_collected == 0.0 and cf.premium_paid == 0.0
    assert abs(cf.net_cash_deployed - 2827.0) < 1e-6
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH=. python3 -m pytest tests/test_reporting_capital_flow.py -q`
Expected: FAIL — `ModuleNotFoundError: autotrader.reporting.capital_flow`.

- [ ] **Step 3: Write minimal implementation**

Create `autotrader/reporting/capital_flow.py`:

```python
"""Pure capital-flow figures for the EOD header, from a day's fills. Options use
the x100 multiplier; stock x1. Never raises."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Tuple

from autotrader.reporting.option_code import parse_option_code


@dataclass(frozen=True)
class CapitalFlow:
    premium_collected: float   # SELL option premium, positive
    premium_paid: float        # BUY option premium, positive
    net_cash_deployed: float   # signed over all legs (SELL +, BUY -)


def compute_capital_flow(fills: Iterable[Tuple[str, str, float, float]]) -> CapitalFlow:
    collected = paid = net = 0.0
    for symbol, side, qty, price in fills:
        parsed = parse_option_code(symbol)
        mult = parsed.multiplier if parsed is not None else 1
        dollars = qty * price * mult
        net += dollars if side == "SELL" else -dollars
        if parsed is not None:
            if side == "SELL":
                collected += dollars
            else:
                paid += dollars
    return CapitalFlow(premium_collected=collected, premium_paid=paid,
                       net_cash_deployed=net)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `PYTHONPATH=. python3 -m pytest tests/test_reporting_capital_flow.py -q`
Expected: PASS (2 passed).

- [ ] **Step 5: Run the full suite + commit**

Run: `PYTHONPATH=. python3 -m pytest tests/ -q` (expect all pass), then:

```bash
git add autotrader/reporting/capital_flow.py tests/test_reporting_capital_flow.py
git commit -m "feat(reporting): capital-flow header figures from fills

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 5: Unrealized P&L plumbing

**Files:**
- Modify: `autotrader/domain.py:159-165`
- Modify: `autotrader/moomoo_broker.py:262-282`
- Modify: `autotrader/sim_broker.py:76-79`
- Modify: `autotrader/db.py` (schema block ~line 70-77; `record_performance` ~line 206-215; `__init__` ~line 113-120)
- Modify: `autotrader/runner.py:46-48`, `autotrader/main.py:88-94`, `autotrader/main.py:405-409`
- Test: `tests/test_db.py` (add a case), `tests/test_unrealized_snapshot.py` (create)

**Interfaces:**
- Produces:
  - `AccountSnapshot.unrealized_pnl: float = 0.0` (new defaulted field).
  - `DB.record_performance(day_pnl, total_assets, cash, gross_exposure, unrealized_pnl=0.0)` (new defaulted trailing param); `performance` table gains an `unrealized_pnl REAL NOT NULL DEFAULT 0` column.
  - Both brokers' `get_account()` set `unrealized_pnl` (Moomoo from `unrealized_pl`; Sim `0.0`).

- [ ] **Step 1: Write the failing test**

Create `tests/test_unrealized_snapshot.py`:

```python
from autotrader.db import DB
from autotrader.sim_broker import SimBroker


def test_account_snapshot_has_unrealized_default_zero():
    snap = SimBroker(quotes={"US.AAPL": 200.0}).get_account()
    assert snap.unrealized_pnl == 0.0


def test_record_performance_persists_unrealized(tmp_path):
    db = DB(str(tmp_path / "p.db"))
    db.record_performance(day_pnl=10.0, total_assets=1000.0, cash=900.0,
                          gross_exposure=100.0, unrealized_pnl=42.5)
    row = db._conn.execute(
        "SELECT day_pnl, unrealized_pnl FROM performance").fetchone()
    assert row[0] == 10.0 and row[1] == 42.5
    db.close()


def test_record_performance_unrealized_defaults_zero(tmp_path):
    db = DB(str(tmp_path / "p.db"))
    db.record_performance(day_pnl=0.0, total_assets=1.0, cash=1.0, gross_exposure=0.0)
    row = db._conn.execute("SELECT unrealized_pnl FROM performance").fetchone()
    assert row[0] == 0.0
    db.close()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `PYTHONPATH=. python3 -m pytest tests/test_unrealized_snapshot.py -q`
Expected: FAIL — `AttributeError: 'AccountSnapshot' object has no attribute 'unrealized_pnl'` (and `record_performance` rejecting the kwarg / missing column).

- [ ] **Step 3a: Add the field to `AccountSnapshot`**

In `autotrader/domain.py`, add to the `AccountSnapshot` dataclass (after `day_pnl: float` on line 163), keeping `positions` last:

```python
@dataclass(frozen=True)
class AccountSnapshot:
    cash: float
    total_assets: float
    day_pnl: float
    stale: bool
    unrealized_pnl: float = 0.0
    positions: Tuple[Position, ...] = ()
```

- [ ] **Step 3b: Set it in both brokers**

In `autotrader/sim_broker.py:76-79` change the return:

```python
    def get_account(self) -> AccountSnapshot:
        positions = tuple(self._positions.values())
        return AccountSnapshot(cash=self._cash, total_assets=self._cash,
                               day_pnl=0.0, stale=False, unrealized_pnl=0.0,
                               positions=positions)
```

In `autotrader/moomoo_broker.py`, after the `pnl = ...` line (272) add:

```python
        upnl = self._c.safe_float(self._c.safe_get(acc.iloc[0], "unrealized_pl", default=0))
```

and pass `unrealized_pnl=upnl` in BOTH `AccountSnapshot(...)` returns (the stale-positions return at ~277 and the normal return at ~281), e.g.:

```python
            return AccountSnapshot(cash=cash, total_assets=total, day_pnl=pnl,
                                   stale=True, unrealized_pnl=upnl, positions=())
        stale = (total == 0 and not positions)
        return AccountSnapshot(cash=cash, total_assets=total, day_pnl=pnl,
                               stale=stale, unrealized_pnl=upnl,
                               positions=tuple(positions))
```

- [ ] **Step 3c: Add the column + ALTER guard + param in `db.py`**

In the `_SCHEMA` `performance` table (db.py ~70-77) add the column:

```python
CREATE TABLE IF NOT EXISTS performance (
    date           TEXT PRIMARY KEY,
    day_pnl        REAL NOT NULL,
    total_assets   REAL NOT NULL,
    cash           REAL NOT NULL,
    gross_exposure REAL NOT NULL,
    unrealized_pnl REAL NOT NULL DEFAULT 0,
    updated_at     TEXT NOT NULL
);
```

In `DB.__init__` (after `self._conn.executescript(_SCHEMA)` ~line 118) add a guarded migration for pre-existing DBs:

```python
        cols = [r[1] for r in self._conn.execute("PRAGMA table_info(performance)")]
        if "unrealized_pnl" not in cols:
            self._conn.execute(
                "ALTER TABLE performance ADD COLUMN unrealized_pnl REAL NOT NULL DEFAULT 0")
        self._conn.commit()
```

Replace `record_performance` (db.py ~206-215):

```python
    def record_performance(self, day_pnl: float, total_assets: float,
                           cash: float, gross_exposure: float,
                           unrealized_pnl: float = 0.0) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO performance "
                "(date,day_pnl,total_assets,cash,gross_exposure,unrealized_pnl,updated_at) "
                "VALUES (?,?,?,?,?,?,?)",
                (_today(), day_pnl, total_assets, cash, gross_exposure,
                 unrealized_pnl, _now()),
            )
            self._conn.commit()
```

- [ ] **Step 3d: Thread `unrealized_pnl` through the three callers**

`autotrader/runner.py:46-48`:

```python
    def _record_perf(self) -> None:
        snap = self._broker.get_account()
        self._db.record_performance(snap.day_pnl, snap.total_assets,
                                    snap.cash, snap.gross_exposure(),
                                    snap.unrealized_pnl)
```

`autotrader/main.py:88-94` — add the kwarg to that `record_performance(...)` call:

```python
            self._db.record_performance(
                day_pnl=snap.day_pnl,
                total_assets=snap.total_assets,
                cash=snap.cash,
                gross_exposure=snap.gross_exposure(),
                unrealized_pnl=snap.unrealized_pnl,
```

(keep the existing trailing argument(s) of that call as-is.)

`autotrader/main.py:405-409`:

```python
            self._db.record_performance(
                day_pnl=snap.day_pnl, total_assets=snap.total_assets,
                cash=snap.cash, gross_exposure=snap.gross_exposure(),
                unrealized_pnl=snap.unrealized_pnl)
```

- [ ] **Step 4: Run tests**

Run: `PYTHONPATH=. python3 -m pytest tests/test_unrealized_snapshot.py tests/test_db.py -q`
Expected: PASS.

- [ ] **Step 5: Run the full suite + commit**

Run: `PYTHONPATH=. python3 -m pytest tests/ -q` (expect all pass — the defaulted field/param keep existing `AccountSnapshot(...)` and `record_performance(...)` call sites valid), then:

```bash
git add autotrader/domain.py autotrader/moomoo_broker.py autotrader/sim_broker.py autotrader/db.py autotrader/runner.py autotrader/main.py tests/test_unrealized_snapshot.py
git commit -m "feat(reporting): persist unrealized P&L from account snapshot

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 6: Strategy-grouped reporter (gather + render + tests)

**Files:**
- Modify: `autotrader/reporting/eod_reporter.py` (replace `ReportData`, `_gather`, `_render`, `_build_blocks`; keep `_default_post`, `__init__`, `send_eod_report`, `_post_with_retry`, `SignalRef`, `DriverRef`, `_pnl_pct`, `_gross_pct`)
- Modify: `tests/test_eod_reporter.py` (rewrite the model/render-dependent tests; keep the retry/send tests)

**Interfaces:**
- Consumes: `parse_option_code`/`ParsedOption` (Task 1), `Leg`/`classify_strategy` (Task 2), `StrategyEconomics`/`compute_economics` (Task 3), `CapitalFlow`/`compute_capital_flow` (Task 4), persisted `unrealized_pnl` (Task 5).
- Produces:
  - `StrategyGroup` (frozen dataclass): `underlying: str`, `label: str`, `legs: Tuple[Leg, ...]`, `economics: StrategyEconomics`, `basis: Optional[float]`, `signal: Optional[SignalRef]`, `driver: Optional[DriverRef]`.
  - `ReportData` fields: `date_label`, `trading_env`, `realized_pnl: Optional[float]`, `unrealized_pnl: Optional[float]`, `total_assets`, `cash`, `gross_exposure`, `capital_flow: Optional[CapitalFlow]`, `groups: Tuple[StrategyGroup, ...]`, `positions: Tuple[Tuple[str,int],...]`.

- [ ] **Step 1: Rewrite the model/render-dependent tests (failing)**

In `tests/test_eod_reporter.py`:

(a) Update the imports at the top to include the new names:

```python
from autotrader.reporting.eod_reporter import EODReporter, ReportData, StrategyGroup
```

(b) Extend `_seed` to also seed an overlay day used by the new tests (append after the existing AAPL seed, before `db._conn.commit()`):

```python
    # Covered-call overlay on CLOV (stock leg + short call) for grouped-report tests.
    db._conn.execute(
        "INSERT INTO fills (fill_id,ts,symbol,side,qty,price) VALUES (?,?,?,?,?,?)",
        ("c1", _DAY + "T14:10:00+00:00", "US.CLOV", "BUY", 200, 4.99))
    db._conn.execute(
        "INSERT INTO fills (fill_id,ts,symbol,side,qty,price) VALUES (?,?,?,?,?,?)",
        ("c2", _DAY + "T14:11:00+00:00", "US.CLOV260821C7000", "SELL", 2, 0.20))
    db._conn.execute(
        "INSERT INTO signals (ts,symbol,direction,confidence,rationale,signal_id) "
        "VALUES (?,?,?,?,?,?)",
        (_DAY + "T14:09:00+00:00", "US.CLOV", "SELL", 0.71,
         "COVERED_CALL: ticker sweep score 71", "sig-cc"))
```

(c) Replace the body of `test_gather_aggregates_fills_and_links_signal` with a grouped-model assertion:

```python
def test_gather_groups_legs_and_links_signal(tmp_path):
    db = DB(str(tmp_path / "r.db"))
    _seed(db)
    r = _reporter(db, lambda url, payload: 200)
    data = r._gather(_now())
    groups = {g.underlying: g for g in data.groups}
    cc = groups["US.CLOV"]
    assert cc.label == "Covered Call"
    assert {l.side for l in cc.legs} == {"BUY", "SELL"}
    assert abs(cc.economics.net_premium - 40.0) < 1e-6
    assert cc.economics.cap == 7.0
    assert cc.signal is not None and abs(cc.signal.confidence - 0.71) < 1e-9
    # rationale's overlay prefix is stripped for the thesis text
    assert cc.signal.rationale == "ticker sweep score 71"
    assert data.realized_pnl == 842.13
    db.close()
```

(d) Replace `test_gather_handles_missing_signal`, `test_gather_links_rebalance_driver_when_no_signal`, `test_signal_takes_precedence_over_rebalance_driver`, and `test_render_shows_rebalance_driver` so they target the AAPL group (which is a `Stock entry` group) instead of `activity[0]`:

```python
def _aapl_group(data):
    return next(g for g in data.groups if g.underlying == "US.AAPL")


def test_gather_handles_missing_signal(tmp_path):
    db = DB(str(tmp_path / "r.db"))
    _seed(db, with_signal=False)
    r = _reporter(db, lambda url, payload: 200)
    g = _aapl_group(r._gather(_now()))
    assert g.signal is None and g.driver is None


def test_gather_links_rebalance_driver_when_no_signal(tmp_path):
    db = DB(str(tmp_path / "r.db"))
    _seed(db, with_signal=False)
    db._conn.execute(
        "INSERT INTO drivers (ts,symbol,side,kind,detail) VALUES (?,?,?,?,?)",
        (_DAY + "T13:58:00+00:00", "US.AAPL", "BUY", "rebalance",
         "rbal-2026-06-16 · underweight → top-up"))
    db._conn.commit()
    g = _aapl_group(_reporter(db, lambda u, p: 200)._gather(_now()))
    assert g.signal is None and g.driver is not None
    assert g.driver.kind == "rebalance" and "underweight → top-up" in g.driver.detail


def test_signal_takes_precedence_over_rebalance_driver(tmp_path):
    db = DB(str(tmp_path / "r.db"))
    _seed(db, with_signal=True)
    db._conn.execute(
        "INSERT INTO drivers (ts,symbol,side,kind,detail) VALUES (?,?,?,?,?)",
        (_DAY + "T13:58:00+00:00", "US.AAPL", "BUY", "rebalance",
         "rbal-2026-06-16 · underweight → top-up"))
    db._conn.commit()
    g = _aapl_group(_reporter(db, lambda u, p: 200)._gather(_now()))
    assert g.signal is not None and g.driver is None


def test_render_shows_rebalance_driver(tmp_path):
    db = DB(str(tmp_path / "r.db"))
    _seed(db, with_signal=False)
    db._conn.execute(
        "INSERT INTO drivers (ts,symbol,side,kind,detail) VALUES (?,?,?,?,?)",
        (_DAY + "T13:58:00+00:00", "US.AAPL", "BUY", "rebalance",
         "rbal-2026-06-16 · underweight → top-up"))
    db._conn.commit()
    text = _reporter(db, lambda u, p: 200)._render(
        _reporter(db, lambda u, p: 200)._gather(_now()))["text"]
    assert "rebalance" in text and "underweight → top-up" in text
    db.close()
```

(e) Replace `test_render_full_report_text_and_blocks` with enriched-header + grouped assertions:

```python
def test_render_enriched_report(tmp_path):
    db = DB(str(tmp_path / "r.db"))
    _seed(db)
    payload = _reporter(db, lambda u, p: 200)._render(
        _reporter(db, lambda u, p: 200)._gather(_now()))
    text = payload["text"]
    assert "Tue, Jun 16 2026" in text and "PAPER" in text
    assert "Realized" in text and "+842.13" in text
    assert "Unrealized" in text
    assert "Premium collected" in text and "Net cash deployed" in text
    assert "Covered Call" in text and "CLOV" in text
    assert "7.00" in text                       # the call strike
    assert "momentum breakout" in text or "ticker sweep score 71" in text
    assert isinstance(payload["blocks"], list) and payload["blocks"][0]["type"] == "header"
    db.close()
```

(f) Update `test_render_quiet_day_heartbeat`'s assertions to the new header wording:

```python
def test_render_quiet_day_heartbeat(tmp_path):
    db = DB(str(tmp_path / "r.db"))
    db._conn.execute(
        "INSERT INTO performance "
        "(date,day_pnl,total_assets,cash,gross_exposure,unrealized_pnl,updated_at) "
        "VALUES (?,?,?,?,?,?,?)",
        (_DAY, 0.0, 100000.0, 100000.0, 0.0, 0.0, _DAY + "T20:30:00+00:00"))
    db._conn.commit()
    payload = _reporter(db, lambda u, p: 200)._render(
        _reporter(db, lambda u, p: 200)._gather(_now()))
    assert "no trades today" in payload["text"].lower()
    assert "100,000" in payload["text"]
    db.close()
```

(g) Leave the `_Capture` class and all `test_send_*` / `test_send_build_failure_does_not_raise` tests UNCHANGED — they assert POST mechanics, not the report model.

- [ ] **Step 2: Run tests to verify they fail**

Run: `PYTHONPATH=. python3 -m pytest tests/test_eod_reporter.py -q`
Expected: FAIL — `ImportError` for `ReportData`/`StrategyGroup` shape and `_gather` still returning the old `activity` model.

- [ ] **Step 3: Rewrite the reporter**

In `autotrader/reporting/eod_reporter.py`:

(a) Update the imports near the top (after the stdlib imports):

```python
from autotrader.reporting.option_code import parse_option_code
from autotrader.reporting.classify import Leg, classify_strategy
from autotrader.reporting.economics import StrategyEconomics, compute_economics
from autotrader.reporting.capital_flow import CapitalFlow, compute_capital_flow
```

(b) Keep `SignalRef` and `DriverRef`. Remove the old `TradeLine`. Add `StrategyGroup` and replace `ReportData`:

```python
@dataclass(frozen=True)
class StrategyGroup:
    underlying: str
    label: str
    legs: Tuple[Leg, ...]
    economics: StrategyEconomics
    basis: Optional[float]
    signal: Optional[SignalRef]
    driver: Optional[DriverRef]


@dataclass(frozen=True)
class ReportData:
    date_label: str
    trading_env: str
    realized_pnl: Optional[float]
    unrealized_pnl: Optional[float]
    total_assets: Optional[float]
    cash: Optional[float]
    gross_exposure: Optional[float]
    capital_flow: Optional[CapitalFlow]
    groups: Tuple[StrategyGroup, ...]
    positions: Tuple[Tuple[str, int], ...]
```

(c) Replace `_gather`:

```python
    _OVERLAY_PREFIXES = ("COVERED_CALL", "PROTECTIVE_PUT", "COLLAR",
                         "BEAR_PUT_SPREAD", "CALL_DIAGONAL", "LEAP")

    @classmethod
    def _strip_overlay_prefix(cls, rationale: str) -> str:
        # Signal rationale for an overlay is stored as "COVERED_CALL: <thesis>".
        head, sep, tail = rationale.partition(": ")
        return tail if (sep and head in cls._OVERLAY_PREFIXES) else rationale

    @staticmethod
    def _underlying_of(symbol: str) -> str:
        parsed = parse_option_code(symbol)
        return parsed.underlying if parsed is not None else symbol

    def _gather(self, now: datetime) -> ReportData:
        today = now.date().isoformat()
        c = self._db._conn
        perf = c.execute(
            "SELECT day_pnl, total_assets, cash, gross_exposure, unrealized_pnl "
            "FROM performance WHERE date=?", (today,)).fetchone()
        fill_rows = c.execute(
            "SELECT symbol, side, SUM(qty) AS q, SUM(qty*price)/SUM(qty) AS avg_price "
            "FROM fills WHERE substr(ts,1,10)=? GROUP BY symbol, side "
            "ORDER BY symbol, side", (today,)).fetchall()

        # Group fills by underlying; each (symbol, side) aggregate becomes one Leg.
        by_under: dict = {}
        for symbol, side, qty, avg_price in fill_rows:
            under = self._underlying_of(symbol)
            parsed = parse_option_code(symbol)
            by_under.setdefault(under, []).append(
                Leg(side=side, qty=qty, price=avg_price, option=parsed))

        groups = []
        for under, legs in by_under.items():
            label = classify_strategy(legs)
            # basis: today's stock-leg avg fill, else position avg_price, else None.
            stock_leg = next((l for l in legs if l.option is None), None)
            if stock_leg is not None:
                basis = stock_leg.price
            else:
                prow = c.execute(
                    "SELECT avg_price FROM positions WHERE symbol=?", (under,)).fetchone()
                basis = prow[0] if prow else None
            econ = compute_economics(legs, basis)
            sig = c.execute(
                "SELECT direction, confidence, rationale FROM signals "
                "WHERE symbol=? AND substr(ts,1,10)=? ORDER BY id DESC LIMIT 1",
                (under, today)).fetchone()
            signal = (SignalRef(sig[0], sig[1], self._strip_overlay_prefix(sig[2]))
                      if sig else None)
            driver = None
            if signal is None:
                drv = c.execute(
                    "SELECT kind, detail FROM drivers "
                    "WHERE symbol=? AND substr(ts,1,10)=? ORDER BY id DESC LIMIT 1",
                    (under, today)).fetchone()
                driver = DriverRef(drv[0], drv[1]) if drv else None
            groups.append(StrategyGroup(
                underlying=under, label=label, legs=tuple(legs), economics=econ,
                basis=basis, signal=signal, driver=driver))

        # Order: largest absolute capital deployed first; stock-only exits last.
        def _deployed(g: StrategyGroup) -> float:
            return sum(abs(l.qty * l.price * (l.option.multiplier if l.option else 1))
                       for l in g.legs)
        groups.sort(key=lambda g: (g.label in ("Stock exit",), -_deployed(g)))

        capital_flow = (compute_capital_flow(
            (r[0], r[1], r[2], r[3]) for r in fill_rows) if fill_rows else None)
        positions = c.execute(
            "SELECT symbol, qty FROM positions WHERE qty != 0 ORDER BY symbol").fetchall()
        return ReportData(
            date_label=now.strftime("%a, %b %d %Y"),
            trading_env=self._env,
            realized_pnl=perf[0] if perf else None,
            unrealized_pnl=perf[4] if perf else None,
            total_assets=perf[1] if perf else None,
            cash=perf[2] if perf else None,
            gross_exposure=perf[3] if perf else None,
            capital_flow=capital_flow,
            groups=tuple(groups),
            positions=tuple((r[0], r[1]) for r in positions),
        )
```

Note: keep `_pnl_pct`/`_gross_pct` but update `_pnl_pct` to read `realized_pnl`:

```python
    @staticmethod
    def _pnl_pct(d: ReportData) -> float:
        base = (d.total_assets or 0.0) - (d.realized_pnl or 0.0)
        return (d.realized_pnl / base * 100) if (base and d.realized_pnl is not None) else 0.0
```

(d) Replace `_render` and `_build_blocks` with strategy-grouped rendering:

```python
    _EMOJI = {"Covered Call": "🟢", "Covered Call (existing shares)": "🟢",
              "Protective Put": "🛡️", "Collar": "🔵", "Bear Put Spread": "🔻",
              "LEAP": "🚀", "PMCC / Call Diagonal": "🚀",
              "Stock entry": "🟢", "Stock exit": "⚪", "Strategy": "▫️"}

    @staticmethod
    def _short(sym: str) -> str:
        return sym.split(".", 1)[1] if "." in sym else sym

    def _leg_line(self, leg: Leg, asof) -> str:
        verb = "Bought" if leg.side == "BUY" else "Sold"
        if leg.option is None:
            return f"{verb} {int(leg.qty)} sh @ {leg.price:,.2f}"
        o = leg.option
        dte = (o.expiry - asof).days
        return (f"{verb} {int(leg.qty)}× ${o.strike:,.2f} {o.right.title()} "
                f"exp {o.expiry:%b %d} ({dte}d) @ {leg.price:,.2f}")

    @staticmethod
    def _econ_line(e: StrategyEconomics) -> Optional[str]:
        parts = []
        if e.net_premium is not None and e.net_premium != 0:
            kind = "credit" if e.net_premium > 0 else "debit"
            parts.append(f"Net {kind} {e.net_premium:+,.0f}")
        if e.cap is not None:
            parts.append(f"cap ${e.cap:,.2f}"
                         + (f" ({e.cap_pct:+.1f}%)" if e.cap_pct is not None else ""))
        if e.floor is not None:
            parts.append(f"floor ${e.floor:,.2f}"
                         + (f" ({e.floor_pct:+.1f}%)" if e.floor_pct is not None else ""))
        if e.hedge_cost_pct is not None:
            parts.append(f"hedge cost {e.hedge_cost_pct:.1f}%")
        return " · ".join(parts) if parts else None

    def _group_text(self, g: StrategyGroup, asof) -> str:
        emoji = self._EMOJI.get(g.label, "▫️")
        conf = f"  conf {g.signal.confidence:.2f}" if g.signal else ""
        head = f"{emoji} {g.label} — {self._short(g.underlying)}{conf}"
        body = [self._leg_line(l, asof) for l in g.legs]
        econ = self._econ_line(g.economics)
        if econ:
            body.append(econ)
        if g.signal and g.signal.rationale:
            body.append(f"Thesis: {g.signal.rationale}")
        elif g.driver:
            body.append(f"driver: {g.driver.kind} · {g.driver.detail}")
        return head + "\n" + "\n".join(f"   {b}" for b in body)

    def _header_lines(self, d: ReportData) -> list:
        lines = [f"Session summary — {d.date_label} ({d.trading_env})"]
        if d.total_assets is not None:
            realized = d.realized_pnl or 0.0
            unreal = "" if d.unrealized_pnl is None else f" · Unrealized {d.unrealized_pnl:+,.2f}"
            lines.append(
                f"Realized {realized:+,.2f} ({self._pnl_pct(d):+.2f}%){unreal} · "
                f"Assets {d.total_assets:,.0f} · Cash {d.cash:,.0f} · "
                f"Gross exp {self._gross_pct(d):.0f}%")
        cf = d.capital_flow
        if cf is not None:
            lines.append(
                f"Premium collected {cf.premium_collected:+,.0f} · "
                f"Premium paid {-cf.premium_paid:+,.0f} · "
                f"Net cash deployed {cf.net_cash_deployed:+,.0f}")
        return lines

    def _render(self, d: ReportData) -> dict:
        asof = datetime.strptime(d.date_label, "%a, %b %d %Y").date()
        lines = list(self._header_lines(d))
        if d.groups:
            leg_count = sum(len(g.legs) for g in d.groups)
            lines.append(f"Activity — {len(d.groups)} strategies, {leg_count} legs")
            for g in d.groups:
                lines.append(self._group_text(g, asof))
        else:
            lines.append("Activity — no trades today")
        pos = " · ".join(f"{sym} {qty}" for sym, qty in d.positions) or "none"
        lines.append(f"Open positions ({len(d.positions)}): {pos}")
        return {"text": "\n".join(lines), "blocks": self._build_blocks(d, asof)}

    def _build_blocks(self, d: ReportData, asof) -> list:
        blocks = [{"type": "header", "text": {"type": "plain_text",
                   "text": f"Session summary — {d.date_label} ({d.trading_env})"}}]
        if d.total_assets is not None:
            realized = d.realized_pnl or 0.0
            fields = [
                {"type": "mrkdwn", "text": f"*Realized*\n{realized:+,.2f} ({self._pnl_pct(d):+.2f}%)"},
                {"type": "mrkdwn", "text": f"*Unrealized*\n{(d.unrealized_pnl or 0.0):+,.2f}"},
                {"type": "mrkdwn", "text": f"*Total assets*\n{d.total_assets:,.0f}"},
                {"type": "mrkdwn", "text": f"*Cash*\n{d.cash:,.0f}"},
                {"type": "mrkdwn", "text": f"*Gross exp.*\n{self._gross_pct(d):.0f}%"},
            ]
            blocks.append({"type": "section", "fields": fields})
        if d.capital_flow is not None:
            cf = d.capital_flow
            blocks.append({"type": "section", "text": {"type": "mrkdwn",
                "text": (f"*Capital flow*\nPremium collected {cf.premium_collected:+,.0f} · "
                         f"Premium paid {-cf.premium_paid:+,.0f} · "
                         f"Net cash deployed {cf.net_cash_deployed:+,.0f}")}})
        blocks.append({"type": "divider"})
        if d.groups:
            for g in d.groups:
                blocks.append({"type": "section", "text": {"type": "mrkdwn",
                               "text": self._group_text(g, asof)}})
        else:
            blocks.append({"type": "section", "text": {"type": "mrkdwn",
                           "text": "*Activity*\nNo trades today"}})
        pos = " · ".join(f"{sym} {qty}" for sym, qty in d.positions) or "none"
        blocks.append({"type": "section", "text": {"type": "mrkdwn",
                       "text": f"*Open positions ({len(d.positions)})*\n{pos}"}})
        return blocks
```

- [ ] **Step 4: Run the reporter tests**

Run: `PYTHONPATH=. python3 -m pytest tests/test_eod_reporter.py -q`
Expected: PASS (all, including the unchanged `test_send_*`).

- [ ] **Step 5: Run the full suite + commit**

Run: `PYTHONPATH=. python3 -m pytest tests/ -q` (expect all pass), then:

```bash
git add autotrader/reporting/eod_reporter.py tests/test_eod_reporter.py
git commit -m "feat(reporting): strategy-grouped EOD summary with enriched P&L header

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Self-Review

- **Spec coverage:** option-code decode (Task 1) · structural classifier (Task 2) · economics: net premium / floor / cap / moneyness / hedge cost (Task 3) · capital-flow header line B (Task 4) · unrealized persistence header A (Task 5) · `_gather` group-by-underlying + latest-signal-per-underlying attribution + rationale-prefix strip + driver fallback + enriched `_render`/`_build_blocks` (Task 6) · parse-only / no correlation_id (no task persists it) · basis from DB only (Task 6 `_gather`) · totality + never-raise preserved (helpers return None/generic; `send_eod_report` try/except untouched). All covered.
- **Placeholder scan:** none — every step carries concrete code/tests. The spec mock's `<mtm>`/`–$X` were illustrative and are not used as plan values.
- **Type consistency:** `ParsedOption`(underlying/expiry/strike/right/multiplier) consistent across Tasks 1-4,6; `Leg`(side/qty/price/option) consistent Tasks 2-3,6; `StrategyEconomics` fields consistent Tasks 3,6; `CapitalFlow` fields consistent Tasks 4,6; `record_performance(..., unrealized_pnl=0.0)` and `AccountSnapshot.unrealized_pnl` consistent Task 5 + callers; `ReportData.realized_pnl`/`unrealized_pnl`/`groups`/`capital_flow` and `StrategyGroup` consistent within Task 6 and its tests; `_pnl_pct` updated to `realized_pnl` in the same task that renames the field.
```
