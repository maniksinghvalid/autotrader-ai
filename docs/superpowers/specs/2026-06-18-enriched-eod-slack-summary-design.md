# Enriched EOD Slack Summary — Design

**Date:** 2026-06-18
**Branch:** `feat/autotrader-paper-v1`
**Status:** Approved (design); pending implementation plan
**Component:** `autotrader/reporting/` (EOD Slack reporter)

## Problem

The end-of-day Slack summary is too shallow to be useful. For a day that deployed
five options strategies it renders a flat list of nine disconnected fills as
cryptic codes with no thesis, no strategy grouping, and no economics:

```
Activity — 9 trade(s)
BUY US.CLOV ×200 @ 4.99
SELL US.CLOV260821C7000 ×2 @ 0.20
SELL US.DIVO260821C47410 ×1 @ 0.30
BUY US.NIO ×200 @ 5.03
SELL US.NIO260731C6000 ×2 @ 0.14
BUY US.NIO260731P4500 ×2 @ 0.16
SELL US.SCHF ×100 @ 28.27
BUY US.SPCE ×100 @ 3.33
BUY US.SPCE260821P3000 ×1 @ 0.48
```

Two root causes in [`eod_reporter.py`](../../../autotrader/reporting/eod_reporter.py):

1. **Attribution silently drops on option legs.** `_gather` joins `fills.symbol`
   to `signals.symbol`, but **signals are keyed by the underlying** (`US.CLOV`)
   while **option-leg fills are keyed by the option code** (`US.CLOV260821C7000`).
   Every option leg fails the join and renders with no thesis.
2. **No notion of a strategy.** The reporter never groups the stock leg + its
   option legs into "Covered Call on CLOV"; it emits a flat list and shows none
   of the economics the legs imply (strike, expiry/DTE, net debit/credit,
   collar floor/cap, hedge cost).

Additionally, the **Day P&L header is misleading**: `day_pnl` is sourced from the
broker's `realized_pl` ([`moomoo_broker.py`](../../../autotrader/moomoo_broker.py)
line ~272 → [`runner.py`](../../../autotrader/runner.py) line 47). A day of
opening trades realizes nothing, so it reads `+0.00` — "nothing happened" — while
ignoring the unrealized mark-to-market of the newly-opened book and the day's
capital flow.

## Decisions

| Decision | Choice | Rationale |
|---|---|---|
| Content shape | **Strategy-grouped** activity (thesis + economics) | The flat list is the core shallowness |
| P&L header | **Realized + Unrealized split AND a capital-flow line** (option C) | Realized alone is misleading on entry days; both together explain the day |
| Grouping/data source | **Parse-only** (reader-side decode + group by underlying) | No schema/write-path change; underlying is the natural daily strategy key. (Two different overlays on the same underlying same day can't be disambiguated — accepted, vanishingly rare.) |
| Strategy label | **Structural classification from parsed legs** (authoritative); signal rationale supplies the *thesis text* only | Doesn't depend on the rationale prefix being present/correct |
| Unrealized source | **Persisted** (`AccountSnapshot` → `performance` table) | Reporter stays SDK/broker-free; reads only the DB |
| Moneyness/basis | From DB (today's stock-leg fill, else `positions.avg_price`) | No live-price lookup needed |

## Target output (built from the example trades)

```
Session summary — Thu, Jun 18 2026 (PAPER)
Realized +0.00 (+0.00%) · Unrealized <mtm> · Assets 1,001,564 · Cash 959,749 · Gross exp 4%
Premium collected +$70 · Premium paid –$48 · Net cash deployed –$X

Activity — 5 strategies, 9 legs

🟢 Covered Call — CLOV                      conf 0.71
   Bought 200 sh @ 4.99, sold 2× $7.00 Call exp Aug 21 (64d) @ 0.20
   Net credit +$40 · capped at $7.00 (+40%) · basis 4.99
   Thesis: ticker sweep score 71 → income overlay

🛡️ Protective Put — SPCE                    conf 0.40
   Bought 100 sh @ 3.33, bought 1× $3.00 Put exp Aug 21 (64d) @ 0.48
   Net debit –$48 · floor $3.00 (–9.9%) · hedge cost 14.4% of notional
   Thesis: HEDGE — downside protection

🔵 Collar — NIO                             conf 0.55
   Bought 200 sh @ 5.03, sold 2× $6.00 Call + bought 2× $4.50 Put, exp Jul 31 (43d)
   Net debit –$4 · floor $4.50 / cap $6.00 · band –10.5% / +19.3%

🟢 Covered Call — DIVO (existing shares)    conf 0.62
   Sold 1× $47.41 Call exp Aug 21 (64d) @ 0.30 · credit +$30

⚪ Exit — SCHF
   Sold 100 sh @ 28.27 (+$2,827) · driver: rebalance trim

Open positions (N): …
```

(Emoji/labels are presentational and tunable later.)

## Components

All new logic lives in small, pure, independently-tested modules under
`autotrader/reporting/`. The reporter composes them; each helper is **total**
(never raises) so the existing "never raise into the loop" guarantee holds.

### 1. `option_code.py` — `parse_option_code(code) -> Optional[ParsedOption]`
Parses a US equity option code: `US.{UNDER}{YYMMDD}{C|P}{strike*1000}`.
- `US.CLOV260821C7000` → `ParsedOption(underlying="US.CLOV", expiry=date(2026,8,21), strike=7.00, right="CALL", multiplier=100)`.
- Strike = trailing integer ÷ 1000. `C`→CALL, `P`→PUT.
- Returns `None` for a plain stock symbol (e.g. `US.SCHF`) or any code that
  doesn't match the pattern. No exceptions.

### 2. `classify.py` — `classify_strategy(parsed_legs) -> str`
Structural label from the set of a single underlying's legs (each leg = side +
ParsedOption-or-None + qty):
- shares(BUY/SELL existing) + short call → `Covered Call`
- shares + long put → `Protective Put`
- shares + short call + long put → `Collar`
- long put + short put (no shares) → `Bear Put Spread`
- two calls (long far + short near) / single long call far-dated → `LEAP / PMCC`
- short call only, no stock leg today → `Covered Call (existing shares)`
- bare stock buy → `Stock entry`; bare stock sell → `Stock exit`
- anything unrecognized → `Strategy` (generic; still grouped and listed)

### 3. `economics.py` — per-strategy figures (pure)
From a group's parsed legs + a `basis` price (today's stock-leg avg fill, else
`positions.avg_price`, else `None`):
- `net_premium`: Σ signed premium dollars (`SELL` +, `BUY` −) × `qty` × `multiplier`.
- `floor` (long-put strike), `cap` (short-call strike) where present.
- `moneyness_pct(strike, basis)`; `hedge_cost_pct = put_premium / (basis*multiplier*contracts)`.
- `dte(expiry, asof)`.
Each figure is `Optional`; missing inputs yield `None` and are simply omitted
from the rendered line.

### 4. `capital_flow.py` — header line B (pure)
From today's fills (using `parse_option_code` to apply ×100 to option legs):
- `premium_collected` (Σ SELL option premiums), `premium_paid` (Σ BUY option premiums),
- `net_cash_deployed` (signed stock + option cash flow; BUY −, SELL +).

### 5. Unrealized P&L plumbing (the only write-path change — header A)
- `domain.AccountSnapshot` gains `unrealized_pnl: float = 0.0` (**defaulted**, so
  existing constructors and the many test `_snap(...)` helpers keep working).
  `MoomooBroker.get_account` sets it explicitly from `unrealized_pl` (safe_get with
  fallbacks); `SimBroker.get_account` sets `unrealized_pnl=0.0`.
- `performance` table gains an `unrealized_pnl` column (additive;
  `CREATE TABLE IF NOT EXISTS` carries it for new DBs + a guarded `ALTER TABLE`
  that no-ops when the column already exists, for existing DBs).
- `DB.record_performance` gains `unrealized_pnl: float = 0.0` (**defaulted**). All
  three current callers that have a snapshot pass `snap.unrealized_pnl`:
  `runner.py:47`, `main.py:90` (overlay path), `main.py:407` (halt path). The
  default keeps any other caller valid.

### 6. Reporter changes (`eod_reporter.py`)
- **`_gather`**: group today's fills by parsed underlying (stock symbol = its own
  underlying). For each group, fetch the **latest signal for that underlying
  today** (drop the brittle `direction` match) → thesis text (rationale with any
  `OVERLAY:` prefix stripped) + confidence; fall back to `drivers` then
  unattributed. Produce a `StrategyGroup`. Read `unrealized_pnl` from `performance`.
- **`_render` / `_build_blocks`**: render the enriched header (realized %, unrealized,
  capital flow) + one Block Kit section per `StrategyGroup` (label, conf, legs,
  economics, thesis), ordered by absolute capital deployed with stock-only exits
  last. Keep the plain-text `text` fallback in sync.

## Data Flow
```
EODReporter._gather(now)
  performance row → realized (day_pnl), unrealized_pnl, assets, cash, gross
  fills(today) → group by parse_option_code(symbol).underlying or symbol
    per group:
      legs = [(side, qty, price, ParsedOption|None)]
      label = classify_strategy(legs)
      basis = today stock-leg avg fill ?? positions.avg_price
      econ  = economics(legs, basis, asof)
      signal = latest signals row for underlying today  → thesis, confidence
               else latest drivers row                  → driver detail
  capital_flow(fills today) → collected / paid / net deployed
→ ReportData(header fields, tuple[StrategyGroup], positions)
→ _render → {text, blocks}  → _post_with_retry (unchanged)
```

## Error Handling
- Every new helper is total; on bad input it returns `None`/generic label, and the
  reporter renders that item as the current flat line rather than dropping it.
- `send_eod_report` retains its outer try/except — it never raises into the runner.
- `ALTER TABLE` for `unrealized_pnl` is guarded so re-running against an existing DB
  is a no-op.

## Testing
- **`option_code`**: the four example shapes (CLOV C7000, DIVO C47410, NIO C6000/P4500,
  SPCE P3000), a plain stock symbol → `None`, malformed code → `None`.
- **`classify`**: one case per strategy shape above, plus the generic fallback.
- **`economics`**: covered-call credit + cap%, protective-put debit + floor% + hedge%,
  collar net + band, missing-basis → figures omitted.
- **`capital_flow`**: mixed stock+option day → collected/paid/net (with ×100 on options).
- **P&L header math**: realized %, unrealized passthrough.
- **brokers**: `unrealized_pnl` present on both `get_account`s.
- **reporter render**: seeded DB (the example day) → grouped blocks + enriched header;
  an unparseable fill falls back to a flat line; `send_eod_report` still never raises.

## Out of Scope
- `correlation_id` / overlay-type persistence on trades (parse-only chosen).
- Live-price lookups (basis comes from the DB).
- Final emoji/label/wording polish (tunable after the structure lands).
- Per-strategy realized P&L on closes (today's realized stays the account figure).
```
