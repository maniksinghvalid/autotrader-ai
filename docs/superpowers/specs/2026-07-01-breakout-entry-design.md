# Design: N-day breakout entry for the internal strategy

**Date:** 2026-07-01
**Status:** Approved (brainstorming)
**Branch:** feat/autotrader-paper-v1

## Problem

The internal `ThresholdStrategy` is wired live in `main()` with a single **static
absolute** `entry_price`, read from `ENTRY_PRICE` (default `0`). With `entry_price=0`
the flat-entry rule `price >= entry_price` is always true, so the strategy opens a
long in its symbol at market the instant the entry window opens (09:45 ET). This was
the root cause of the unexplained daily "buy a semi-random holding at open" behavior
(MARA on 2026-07-01, AAPL/IBIT/IAU on other days). An absolute price is also
unmanageable: it must be hand-updated as the market moves, and it means nothing
across a multi-symbol, webhook-driven book.

The internal strategy is intended to remain live (it shares the audited
`_route_signal` path with webhook + rebalance signals), so the fix is to replace the
static price with an **intelligent, self-adjusting entry rule** and to make the
"unconfigured / no data" case **fail safe** (no entry), never buy-at-open.

## Goal

Replace `entry_price` with an **N-day breakout** entry: buy when price makes a new
N-day high. Keep the strategy pure/stateless; make any data problem fail safe to
*no entry*; retire `ENTRY_PRICE`.

## Non-goals

- Multi-symbol internal strategy (still one symbol: the deterministic
  `select_strategy_symbol` pick, already shipped in `952a764`).
- Changing exit logic (stop −5% / take-profit +10% stay).
- Changing webhook / rebalance / risk-core behavior.
- Retiring `ThresholdStrategy` from the tree (it stays, tested, just no longer wired
  live; a separate cleanup may remove it later).

## Components (four small, independently testable units)

### 1. `BreakoutStrategy` (new, pure — replaces `ThresholdStrategy` in `main()`)

`evaluate(price, position, ref_high) -> Signal | None`, stateless per CLAUDE.md — the
reference is passed in, never fetched by the strategy:

- **Flat + `ref_high` is a number + `price > ref_high`** → `BUY` (rationale names the
  breakout, e.g. `"breakout: 131.20 > 20d-high 130.50"`).
- **Flat + `ref_high is None`** → `None` (fail-safe: no reference, no entry).
- **Flat + `price <= ref_high`** → `None`.
- **Holding** → unchanged exits: stop-loss `change <= -stop_loss_pct` → `SELL`;
  take-profit `change >= take_profit_pct` → `SELL`; else `None`.

Breakout is **strict `>`**. Params: `stop_loss_pct` (0.05), `take_profit_pct` (0.10),
`confidence` (0.7) — same as today. No `entry_price`. Exit logic is shared with
`ThresholdStrategy` via a small private helper to avoid duplication.

### 2. `BreakoutReference` (new, stateful — the daily cache)

`high(symbol) -> float | None`. The **only** new stateful piece. Holds
`broker`, `lookback`, `today_fn` (clock).

- Computes the N-day high **once per trading day** (cache keyed by `today_fn()`);
  returns the cached value on repeat calls the same day.
- Recomputes when the date rolls.
- A `None` result (see fail-safe) is **not cached**, so the next tick retries —
  self-healing a transient failure before entries open at 09:45.
- Warns at most **once per day per symbol** on a `None` result (avoids 5 s-loop log
  spam); tracks the last-warned day.

### 3. `Broker.recent_high(symbol, lookback) -> float | None` (new interface method)

Added to the `Broker` interface (`broker.py`).

- `MoomooBroker`: fetch the last `lookback` **completed** daily klines (exclude
  today's forming bar), return `max(high)`. Return `None` on `ret_code != RET_OK`,
  insufficient bars (`< lookback` completed), or a caught exception (checked +
  logged per CLAUDE.md, never swallowed). Uses the vendored moomoo kline path;
  guarded by the existing `rate_limiter`.
- `SimBroker`: returns a configured value (or `None`) for tests.

### 4. `TradeEngine.tick()` wiring

- If the internal strategy is **disabled** (`STRATEGY_ENABLED=false`) → return
  `TickResult("STRATEGY_DISABLED")` for the internal tick. External signals,
  rebalance, and risk checks still run (they are driven elsewhere in the loop).
- Else: `ref_high = self._breakout_ref.high(symbol)`, then
  `self._strat.evaluate(price, pos, ref_high)`. Everything downstream
  (`_route_signal` → confidence filter → entry gate → risk core → router) is
  unchanged.

## Data flow

```
first tick of the trading day
  -> BreakoutReference.high(symbol)         (daily cache miss)
     -> Broker.recent_high(symbol, N)
        -> last N completed daily klines -> max(high)  (or None on any problem)
     -> cache[(today, symbol)] = high       (only on success)
  -> TradeEngine.tick(): evaluate(price, pos, ref_high)
     -> price > ref_high  ⇒  BUY  -> _route_signal -> risk core -> OrderRouter
```

## Configuration

All signal-shaping (not risk limits), so read in `main()` and passed to the
components — kept **out** of `RiskConfig` (reserved for human-reviewed risk limits
per CLAUDE.md).

| Env var | Type | Default | Meaning |
|---|---|---|---|
| `ENTRY_BREAKOUT_LOOKBACK` | int | `20` | N completed daily bars for the high. Clamped to ≥1 (invalid → warn + 20). Lives on `BreakoutReference`. |
| `STRATEGY_ENABLED` | bool | `true` | Kill switch. `false` → `tick()` skips the internal strategy; webhook + rebalance + risk checks still run. |
| ~~`ENTRY_PRICE`~~ | — | **retired** | Removed from `main()`, along with the `ENTRY_PRICE<=0` startup warning added in `952a764`. |

Kept as-is: `STRATEGY_SYMBOL` + `select_strategy_symbol` (single-symbol pick), the
`sig-<session>` ids, exits (−5% / +10%), confidence (0.7).

## Fail-safe rules (the whole point)

- `recent_high` → `None` on non-OK ret code, insufficient bars, or exception.
- `BreakoutReference.high` returns `None` **uncached** (retried next tick); warns
  once/day/symbol.
- `evaluate(ref_high=None)` → `None`. **No absolute-price fallback exists anywhere** —
  any data problem means "don't enter this tick," never "buy at open."
- Today's forming bar is excluded from the high, so `price > high` is a true breakout
  of the prior N-day range.

## Error handling

- Every moomoo call checks `ret_code == RET_OK`, logs non-OK with context, returns
  `None` (CLAUDE.md). No bare `except: pass`.
- Retries on `None` are throttled by the existing `rate_limiter`; on persistent
  failure the strategy simply does not trade that day (logged once).

## Testing (real code — SimBroker / fakes + FixedClock, no OpenD)

- **`BreakoutStrategy`** (pure): `price>ref`→BUY; `price==ref`→None; `price<ref`→None;
  `ref=None`→None; holding→stop SELL / take-profit SELL / in-between None.
- **`BreakoutReference`**: fetches once/day (2nd same-day call = cache hit);
  recomputes on date-roll; `None` result uncached and retried; warn-once-per-day.
- **`Broker.recent_high`**: SimBroker returns configured/None; `MoomooBroker` offline
  test mocks kline `RET_OK`→max-high, `RET_ERROR`→None, insufficient→None (mirrors
  `test_moomoo_broker_offline`).
- **`tick()` integration**: enabled+breakout→`ORDER_PLACED`; enabled+no-breakout→
  `NO_SIGNAL`; enabled+`ref=None`→no entry; disabled→`STRATEGY_DISABLED` with the
  external-signal path still working.
- **Config**: lookback default/clamp; `STRATEGY_ENABLED` default-true/false parsing.
- **Docs**: RUNBOOK updated — drop the `ENTRY_PRICE` rows + the "bot buys at open"
  troubleshooting entry; add `ENTRY_BREAKOUT_LOOKBACK`, `STRATEGY_ENABLED`, and the
  fail-safe note.

## Relationship to current work

The `sig-<session>` fix + deterministic-symbol hardening are committed (`952a764`),
on top of the deferral commit (`7966621`). This breakout feature builds on that: it
removes `ENTRY_PRICE` and its startup warning, keeps `select_strategy_symbol`, and
adds the four units above.
