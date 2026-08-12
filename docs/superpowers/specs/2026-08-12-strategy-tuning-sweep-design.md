# Strategy Tuning: Rule Variants + Parameter Sweep (OOS-validated)

## Context

The user wants to tweak the AutoTrader investment strategy and backtest it targeting **~80% win rate with ≤5% max drawdown**. The run of record (7 portfolio equities, 2023→today, production knobs) scored 31% win rate — and that is structural to breakout systems, not a tuning artifact. The user approved (via AskUserQuestion): **(a)** parameter tuning PLUS new production-quality rule variants, **(b)** out-of-sample validation — optimize on 2023-01-01→2024-12-31, confirm on 2025-01-01→today; the targets only "count" if they hold out-of-sample.

**Honest expectations (stated up front, will be restated with results):** no backtest can *ensure* future performance, and this is research tooling, not investment advice. Realistically: breakout even with a regime filter plausibly reaches 40–50% win rate; the pullback (mean-reversion) family is the only one that can plausibly reach **60–75% validated** win rate — by construction it trades win frequency for negative skew (average loss > average win). A row showing ~80% must be inspected for profit factor < ~1.2 and near-minimum trade counts before being believed. If nothing meets both targets out-of-sample, the deliverable is the honest frontier — the best validated configs and their trade-offs — not a curve-fit number.

## What exists (built earlier this session, 800 tests green)

- `autotrader/backtest/engine.py` — bar-walking simulator, 8 documented pessimistic intrabar fill rules, warmup handling (bars before `cfg.start` feed indicators only; bars after `cfg.end` dropped — so train/val windows need **no bar slicing**, just different start/end on the same full bar list). Currently hardcodes `BreakoutStrategy` + rolling-high. **Measured 6ms per 2-year 7-symbol run** → 594-combo sweep ≈ 4s serial, no parallelism needed.
- `autotrader/backtest/{data,metrics,report,__main__}.py` — Massive client + cache (cache currently starts 2022-11-20), round-trip pairing + `summarize()`, HTML report, CLI. Dashboard Backtests tab lists any run dir containing `summary.json` — the sweep's winner appears with **zero dashboard changes**.
- `autotrader/strategies/breakout.py` (`evaluate(price, position, ref_high)`), `strategies/exits.py::manage_long_exit` (SL-before-TP), CLAUDE.md hard rules: strategies stateless with explicit SL/TP; no SDK in core (`tests/test_no_sdk_in_core.py` enumerates modules by hand).

## Design

### 1. Strategy variants (production-quality, in `autotrader/strategies/`)

No shared `Refs` dataclass (each variant needs different refs, and the engine doesn't route entries through `evaluate` anyway — see §2). Signatures stay minimal:

- **`breakout.py` (modified, backward-compatible):** `BreakoutStrategy.evaluate(price, position, ref_high=None, sma=None)` — entry requires `price > ref_high` AND (`sma is None or price > sma`). `sma=None` default keeps every existing caller/test/production `main.py` byte-identical. "breakout_regime" = same class with `sma` supplied.
- **NEW `pullback.py` (~45 lines mirroring breakout.py):** `PullbackParams(symbol, stop_loss_pct, take_profit_pct, confidence)` with the same `__post_init__` validation (explicit SL/TP enforced — CLAUDE.md). `PullbackStrategy.evaluate(price, position, ref_low=None, sma=None)`: held → `manage_long_exit`; flat → BUY iff `sma is not None and price > sma` (uptrend regime) AND `ref_low is not None and price <= ref_low` (the dip). Any missing ref → no entry (fail-safe, matching breakout's None guard).

### 2. Engine generalization (`autotrader/backtest/engine.py`)

Key insight from design review: **the engine calls `strategy.evaluate` only for the Rule-1 open-price exit; entry triggers are engine-side in `process_entry`** (stop-buy fill mechanics need the bar's OHLC). So variant entries are an engine dispatch, and each variant gets a **parity test** asserting the strategy class's entry predicate agrees with the engine trigger (the rule intentionally exists in two places).

- `BacktestConfig` gains frozen-default fields (existing call sites unaffected): `strategy: str = "breakout"` (`breakout|breakout_regime|pullback`), `regime_sma: int = 200`, `pullback_lookback: int = 10`, `pullback_depth_pct: float = 0.0`.
- `_SymState` (`__slots__` — new fields must be added to the slots tuple) gains `forming_low`, `forming_close`, `daily_lows: deque(maxlen=pullback_lookback)`, `closes: deque(maxlen=regime_sma)`; day-roll appends all forming values; warmup loop updates them. New helpers `ref_low(m)`, `sma(r)` — None until the window is full (same contract as `ref_high`).
- Fix the Rule-1 call: `evaluate(o, Position(...), None)` → `evaluate(o, pos)` (keyword defaults; `PullbackStrategy` has no `ref_high` param).
- Strategy factory `_make_strategy(cfg, symbol)`; unknown name → `ValueError`.

**New intrabar fill rules (appended to docs/BACKTESTING.md as Rules 9–12; no look-ahead):**
- **Rule 9 (pullback trigger level):** `T = min(last M completed daily lows)` when `pullback_depth_pct == 0`, else `T = SMA_R·(1 − depth)`. Computed from completed data only; insufficient history → `T = None` → no entry.
- **Rule 10 (regime gate):** entry allowed only if the previous completed day's close `> SMA_R` (both operands completed data; the current bar never consulted). Same gate ANDed in front of Rule 5 for breakout_regime.
- **Rule 11 (limit-buy fill):** if `bar.low <= T` → `fill_px = min(bar.open, T)` (a resting buy-limit fills at the open when the open is below the limit — correct, not optimistic), slippage `fill_px·(1+slip)`. `bar.low > T` → no entry. Existing Rule 7 entry window applies on intraday timespans.
- **Rule 12 (same-bar exit after pullback entry):** identical to Rule 6 — same-bar SL/trailing may fire off `p_e`; same-bar TP never.

Rule 5 gating (no re-entry same bar, no pyramiding, exits before entries) applies unchanged.

### 3. Sweep harness (NEW `autotrader/backtest/sweep.py`, run as `python -m autotrader.backtest.sweep`)

- **Data:** fetch ONCE per symbol via `load_bars` with `fetch_start = train_start − ceil(max_indicator_window·1.6 + 10) days` ≈ **2022-02-05** for SMA(200) — the existing cache (starts 2022-11-20) is NOT sufficient; without this, SMA(200) configs silently don't trade until ~Oct 2023 and corrupt the train comparison. One new API hit per symbol, cached thereafter. Pass the full bar list to both windows (engine's start/end semantics handle warmup/truncation — verified).
- **Grid (explicit lists, 594 combos ≈ 4s):**
  | Variant | Axes | Count |
  |---|---|---|
  | breakout | lookback {10,20,40,55} × SL {.02,.03,.05} × TP {.03,.05,.10} × trail {0,5,8} | 108 |
  | breakout_regime | same × regime_sma {100,200} | 216 |
  | pullback | M {5,10,15} × depth {0,.02,.05} × SL {.02,.03,.05} × TP {.02,.03,.05} × regime_sma {100,200} | 270 |
- **Structure:** `run_cell(bars, base_cfg, overrides, start, end, risk_cfg) -> dict` (builds `dataclasses.replace`d config, runs `run_backtest`+`pair_round_trips`+`summarize`, returns flat row); `sweep(...) -> (train_rows, topk_rows)` pure and testable without I/O; `main(argv, client_factory=None)` mirroring `__main__.py` conventions (`--symbols` default = the 7 portfolio tickers; `--train-from/--train-to/--val-from/--val-to` defaults 2023-01-01/2024-12-31/2025-01-01/today; `--max-dd 0.05`, `--min-train-trades 30`, `--min-val-trades 8`, `--top-k 10`, cost flags, `--out-dir`). Reuses `_write_equity_csv`/`_write_trades_csv`/`_print_summary` from `__main__.py`.
- **Objective — lexicographic constraint ranking, not a distance score** (a composite score hides which constraint failed; the user's deliverable is an honest frontier):
  1. Train filter: `trade_count ≥ 30` AND `max_drawdown ≤ 0.05`. Excluded rows still written to `train_grid.csv` with `excluded_reason`.
  2. Train rank: win_rate desc, tie-break Sharpe. Take **top-K per variant family** (K=10 → ≤30 candidates; per-family cap = multiple-testing control so one family can't flood validation).
  3. Validation: candidates need `trades ≥ 8` AND `max_drawdown ≤ 0.05` **on validation** (the constraint counts out-of-sample).
  4. Final ranking by **validation** win_rate (tie Sharpe); every row reports `gap = train_win_rate − val_win_rate` (overfit indicator) + val profit factor + avg win/loss (win rate is gameable by tiny-TP configs — PF makes a "78% win rate, PF 0.9" row visibly worthless).
  5. Run of record: best validated config re-run over the **validation window only** (full period would mix in-sample data into headline numbers) through the standard report path into `~/.autotrader/backtest_runs/<stamp>_sweep-best-<strategy>/` → appears in the dashboard automatically.
- **Outputs:** `train_grid.csv` (params + metrics + excluded_reason), `validation_topk.csv` (params + train/val metrics + gap), stdout top-K table, run-of-record dir. If nothing survives validation constraints → print the nearest-miss frontier (best val win-rate rows with the violated constraint named) — that IS the honest deliverable.

### 4. Files changed/added
- MODIFY `autotrader/backtest/engine.py` (config fields, indicators, entry dispatch, Rule-1 call fix), `autotrader/strategies/breakout.py` (optional `sma` kwarg), `tests/test_no_sdk_in_core.py` (+`autotrader.strategies.pullback`, `autotrader.backtest.sweep`), `docs/BACKTESTING.md` (Rules 9–12 + sweep usage).
- NEW `autotrader/strategies/pullback.py`, `autotrader/backtest/sweep.py`, `tests/test_pullback_strategy.py`, `tests/test_backtest_engine_variants.py`, `tests/test_backtest_sweep.py`.
- **Phase 2 (deliberately out of scope, after user reviews sweep results):** wiring the winning variant into production `main.py`/`moomoo_broker.py` (needs a recent-low/SMA fetch analogous to `recent_high`). Not designed yet — don't build production plumbing for a variant that hasn't won.

### 5. Tests (one-line invariants)
- Pullback strategy: dip+uptrend → BUY; dip in downtrend → None; missing refs → None; held → SL-before-TP; SL/TP ≤ 0 rejected.
- Breakout `sma` kwarg: omitted → byte-identical behavior (existing tests pass untouched); sma above price blocks an otherwise-valid breakout.
- Engine indicators: SMA/ref_low from completed days only (day N's decision ignores day N's own bar); warmup feeds lows/closes.
- Pullback fills: open<T → fill at open; open>T≥low → fill at T; low>T → none; regime gate uses previous completed close (mutating current bar's close doesn't flip it).
- Same-bar pessimism: pullback entry→same-bar SL fires; same-bar TP never.
- Entry parity per variant: strategy-class predicate at close prices ≡ engine trigger.
- Sweep: no-leak (mutating bars after train_end leaves train rows identical); degenerate filter + reason; objective known-answer; per-family top-K cap; `run_cell` roundtrips overrides.

## Verification
1. `uv run pytest -q --ignore=tests/test_moomoo_broker_live.py --ignore=tests/test_inbox_ordering_targets.py` — all green (existing 800 + new; the inbox file is a pre-existing unrelated failure).
2. Offline sweep smoke via injected fake client in tests.
3. Real run: `python -m autotrader.backtest.sweep` (sourced secure.config already in place) over the 7 portfolio tickers with default windows; deliver stdout frontier + CSVs + dashboard-visible run of record; report validated numbers with the expectations framing above.
