# Phase 2: Wire the Pullback Strategy into Production

## Context

The OOS-validated sweep found a pullback configuration that met the user's targets out-of-sample (81.5% validated win rate, 0.93% max drawdown, PF 2.00: dip to the 15-day low, above the 100-day SMA, SL 5% / TP 2%). `PullbackStrategy` already exists as production-quality code (`autotrader/strategies/pullback.py`, stateless, explicit SL/TP) but only the backtest engine can drive it — the live loop (`autotrader/main.py`) hardcodes breakout wiring. This plan makes the internal strategy **selectable** (`STRATEGY_KIND=breakout|pullback`, default `breakout` → zero behavior change unless opted in) and gives the broker layer the reference data a pullback needs. Paper trading only, as always; enabling the pullback in the live config remains a human decision after this ships.

## Verified wiring facts (from exploration; file:line anchors current)

- **The one hard blocker:** `main.py:168` calls `self._strat.evaluate(price=, position=, ref_high=)` — keyword-bound to breakout's signature; `PullbackStrategy.evaluate` has no `ref_high` → TypeError.
- Strategy surface in the engine is tiny: `self._strat` at `main.py:78,155,168` only. `tick()` (149–175) is otherwise strategy-agnostic; `_route_signal` routes internal signals with `origin=books.ORIGIN_BREAKOUT` (175).
- **Claims/origin: keep `ORIGIN_BREAKOUT` for the internal book regardless of kind.** Verified: rebalance filtering (835–840) and `reconcile_claims` (982–996) key off symbol membership only; no report/query groups by origin; only one internal strategy runs per process so two books never coexist. Renaming the origin would require touching `main.py:165/256/322/324/347` + `books.py` — and getting `main.py:165` wrong strands open positions as `POSITION_NOT_OWNED` after a restart. Cosmetic cost (claim rows say BREAKOUT) documented in `db.py:105` comment + RUNBOOK.
- `breakout_reference.py` (36 lines) is 100% cache/warn/fail-safe plumbing, 0% breakout logic (only line 27 is breakout-specific). Explorer verdict: parameterize the fetch, don't clone the class (drift risk).
- `moomoo_broker.recent_high` (126–164) requests full kline frames — **`low` and `close` columns are already in every response** (`get_kline.py:147-154`); it just ignores them. Quote-context path has no trade rate-limit tokens. Windowing: `start = today − (2·need+10)d`, `max_count = 2·need+15`.
- `SimBroker` injects `recent_highs` as a dict (ctor :19 → getter :65-66); **every** engine-level test double subclasses SimBroker, so new methods propagate for free. Only hand-rolled fake is in `test_breakout_reference.py` (duck-typed).
- Test patterns to follow: breakout tick tests at `tests/test_main_loop.py:304-341` (inline TradeEngine + SimBroker + BreakoutReference with `today_fn`), pass-through assertion at :354-364.
- The backtest engine already has the selection precedent: `BacktestConfig.strategy`, `_make_strategy`, `regime_ok` using previous completed close.

## Design

### 1. Reference layer — generalize the per-day cache (`autotrader/breakout_reference.py`)
- Extract the cache/warn-once/None-not-cached mechanics into a small internal `_DailyRefCache(fetch, today_fn)` with `value(symbol)`; `fetch(broker, symbol) -> value | None`, computed once per trading day, `None` never cached (retried next tick), warn once per day.
- `BreakoutReference` keeps its public shape (`high(symbol)`) delegating to the cache — existing tests/callers untouched — and gains `kwargs(symbol) -> {"ref_high": ...}`.
- New `PullbackReference(broker, low_lookback, sma_window, today_fn=None)` in the same module: fetch returns the tuple `(ref_low, sma)` via the new broker methods, **all-or-nothing** — if either leg is None the whole value is None (a missing SMA must never produce a half-gated entry). `kwargs(symbol)` -> `{"ref_low": lo, "sma": sm}` (both None when the value is None → strategy's fail-safe no-entry).

### 2. Engine — strategy-shape-agnostic tick (`autotrader/main.py`)
- Rename the `breakout_ref` param/attr to `strategy_ref` (TradeEngine.__init__ :80, build_engine :1010-1030, and the 3 inline test constructions + pass-through test). A misnamed load-bearing param in the order path isn't worth keeping.
- `tick()` :167-168 becomes:
  `ref_kwargs = self._strategy_ref.kwargs(symbol) if self._strategy_ref is not None else {}` then `signal = self._strat.evaluate(price=price, position=pos, **ref_kwargs)`.
- `strategies/threshold.py`: ensure `evaluate`'s reference params have defaults so the empty-kwargs path works for threshold engines (1-line change if missing; verify during implementation).
- Origin/claims: unchanged (`ORIGIN_BREAKOUT` = "the internal book's token"); update the misleading log line at :1081 to name the kind, and the `db.py:105` schema comment.

### 3. Broker data (`broker.py`, `moomoo_broker.py`, `sim_broker.py`)
- `moomoo_broker.py`: factor the fetch + forming-bar exclusion out of `recent_high` into `_completed_daily_bars(symbol, need) -> list[rows] | None` (same windowing math, None on any failure); `recent_high` becomes a thin consumer. Add:
  - `recent_low(symbol, lookback) -> float | None` — min of last `lookback` completed lows; None on failure/short history (mirrors recent_high exactly).
  - `sma(symbol, window) -> float | None` — mean of last `window` completed closes; same contract.
- `broker.py`: declare both next to `recent_high` with the same docstring contract ("None fails safe upstream to 'no entry' — never a buy-at-open").
- `sim_broker.py`: two ctor dicts (`recent_lows=None`, `smas=None`) + two 1-line getters mirroring `recent_highs` (:19, :42, :65-66). All SimBroker-subclass test fakes inherit them.

### 4. Selection + config (`main.py` `main()`, config, docs)
- New env `STRATEGY_KIND` (default `breakout`; values `breakout` | `pullback`; unknown → refuse to start with a clear error — never guess in the order path). Pullback knobs: `ENTRY_PULLBACK_LOOKBACK` (default 15), `REGIME_SMA` (default 100). SL/TP/confidence reuse the existing `STRATEGY_STOP_LOSS_PCT` / `STRATEGY_TAKE_PROFIT_PCT` / `STRATEGY_CONFIDENCE`.
- Extract strategy construction from `main()` into a testable helper `build_internal_strategy(symbol) -> (strategy, ref_factory)` (or equivalent small function) so kind-selection is unit-testable without OpenD; `main()` calls it, connects the broker, builds the chosen reference, logs `internal strategy: %s symbol=%s ...` with the real kind.
- `config/risk.config.example`: extend the STRATEGY block — `STRATEGY_KIND` + pullback knobs, plus a **commented** "validated 2026-08-12 sweep config" block (`STRATEGY_KIND=pullback`, `ENTRY_PULLBACK_LOOKBACK=15`, `REGIME_SMA=100`, `STRATEGY_STOP_LOSS_PCT=0.05`, `STRATEGY_TAKE_PROFIT_PCT=0.02`) with the OOS numbers and the note that the validated config ran with **no trailing stop** (`RISK_TRAILING_STOP_PCT=5.0` in the live config WILL add trailing exits on top — behavior the sweep didn't measure). Live `config/risk.config` is NOT edited — risk values are human-review-only per CLAUDE.md.
- `RUNBOOK.md` §3 table: rows for `STRATEGY_KIND`, `ENTRY_PULLBACK_LOOKBACK`, `REGIME_SMA`; the "Breakout entry" callout (:144) becomes strategy-conditional prose. `autotrader/README.md` one-line mention.

### 5. Backtest CLI parity (`autotrader/backtest/__main__.py`)
Add `--strategy` (env `STRATEGY_KIND`, default breakout), `--regime-sma` (env `REGIME_SMA`, default 100... engine default stays 200 — CLI default reads env then 200 to match engine), `--pullback-lookback` (env `ENTRY_PULLBACK_LOOKBACK`, default 15 via env else 10 engine default — pick ONE: flag defaults = engine defaults, env overrides), `--pullback-depth-pct`. This lets the user reproduce the winning config with the plain backtest CLI and keeps the "same env vars as production" parity story true for the new knobs. Exact default resolution: flag default = `os.getenv(<env>, <engine default>)`, matching the existing lookback/SL/TP pattern.

## Files changed/added
- MODIFY `autotrader/main.py` (tick ref_kwargs, strategy_ref rename, STRATEGY_KIND selection helper, log line), `autotrader/breakout_reference.py` (cache extraction + PullbackReference + kwargs), `autotrader/moomoo_broker.py` (`_completed_daily_bars` + `recent_low` + `sma`), `autotrader/broker.py` (port methods), `autotrader/sim_broker.py` (dicts + getters), `autotrader/strategies/threshold.py` (defaults if needed), `autotrader/db.py` (comment only), `autotrader/backtest/__main__.py` (variant flags), `config/risk.config.example`, `RUNBOOK.md`, `autotrader/README.md`.
- NEW tests: `tests/test_pullback_reference.py`; additions to `tests/test_main_loop.py`, `tests/test_moomoo_broker_offline.py`, `tests/test_sim_broker.py`, `tests/test_backtest_cli.py`.
- NO changes: `books.py`, `db.py` queries, `runner.py`, `reporting/*`, `risk_core.py`, `stops.py` (trailing attach and simulated stops are strategy-agnostic and apply to pullback entries unchanged).

## Tests (one-line invariants)
- moomoo offline (fake kline ctx, existing `_FakeKlineQuoteCtx` pattern): `recent_low` = min of last N completed lows, forming bar excluded; `sma` = mean of last R completed closes; short history / non-OK ret / exception → None for both; one shared fetch path (`_completed_daily_bars`) drives all three methods.
- sim_broker: injected `recent_lows`/`smas` dicts returned; missing symbol → None.
- pullback reference: computed once per day (fetch counted); recomputed on date roll; None not cached and retried; **either leg None → whole value None**; `kwargs()` shapes for both reference classes.
- main loop: pullback engine enters on dip-in-uptrend (`ORDER_PLACED` with origin BREAKOUT claim recorded); `ref_low`/`sma` missing → `NO_SIGNAL` (fail-safe); dip below SMA (downtrend) → `NO_SIGNAL`; breakout engines byte-identical via renamed `strategy_ref` (existing tests updated mechanically); threshold engine with no ref still works (empty kwargs); `STRATEGY_KIND=pullback` selection helper builds PullbackStrategy + PullbackReference, unknown kind raises.
- backtest CLI: `--strategy pullback --pullback-lookback 15 --regime-sma 100` flows into `BacktestConfig`; env fallbacks honored.

## Verification
1. `uv run pytest --ignore=tests/test_moomoo_broker_live.py --ignore=tests/test_inbox_ordering_targets.py` — all green (840 + new).
2. Reproduce the winning validation run through the CLI as an end-to-end check: `python -m autotrader.backtest --symbols CLOV,DIVO,IAU,IBIT,NIO,O,SCHF --from 2025-01-01 --to 2026-08-12 --strategy pullback --pullback-lookback 15 --regime-sma 100 --stop-loss-pct 0.05 --take-profit-pct 0.02 --trailing-stop-pct 0 --commission 1.0 --slippage-bps 5` → must match the sweep's run of record (81.5% win rate, 0.93% DD).
3. Production smoke is config-gated and left to the user: set `STRATEGY_KIND=pullback` (+ knobs) in their sourced config and start the trader against OpenD paper; startup log must name the pullback strategy and its knobs. Not run automatically — changing the live strategy config is a human-review action (CLAUDE.md).
