# Strategy Backtesting Engine (Massive API)

## Context

AutoTrader trades a single-symbol N-day breakout strategy (paper) against Moomoo, but has no way to evaluate that strategy against history — no backtest code, no historical-bar infrastructure, no candle type anywhere in the repo. This plan adds a reusable backtesting engine inside the existing project that (a) reuses the production decision logic so results reflect real AutoTrader behavior, (b) pulls OHLCV history from the Massive API (formerly Polygon.io; user has a key — placeholder goes in config, never hardcoded), (c) is configurable (tickers, dates, interval, capital, costs), (d) produces standard performance metrics plus a trade-by-trade record and equity curve, and (e) ships with tests, a CLI, and docs. Implemented end-to-end per the user's request.

## What we reuse vs. build (verified by exploration)

**Reused production logic (pure, offline-importable — verified with no SDK present):**
- `autotrader/strategies/breakout.py` — `BreakoutStrategy.evaluate(price, position, ref_high)`: flat + `price > ref_high` (strict, line 43) → BUY. `ref_high` = max high of last N *completed* daily bars, N = `ENTRY_BREAKOUT_LOOKBACK` (default 20); missing data → None → no entry (fail-safe).
- `autotrader/strategies/exits.py::manage_long_exit` — vs `avg_price`: ≤ −`STRATEGY_STOP_LOSS_PCT` (0.05) → SELL stop-loss; ≥ `STRATEGY_TAKE_PROFIT_PCT` (0.10) → SELL take-profit. SL checked before TP (lines 19–22). Full-position sells.
- `autotrader/sizing.py::size_position` — production sizing incl. confidence scaling and caps (risk mode). Note: fixed-qty mode (`RISK_PER_TRADE_PCT=0`) early-returns UNCAPPED (`FIXED_DISABLED`) — engine adds its own notional/qty/cash clamp for that path.
- Trailing-stop semantics from `autotrader/stops.py::check_simulated` (172–237): HWM **reseeds each morning to the morning quote** (live-parity; overnight gap never fires the stop by itself), ratchets up intraday, fires at hwm·(1−`RISK_TRAILING_STOP_PCT`/100). Percent, not fraction; 0 = off. Replicated in the engine (needs bar-domain reimplementation; the class needs a DB + broker).
- Domain `Position` from `autotrader/domain.py`; `RiskConfig`/`load_risk_config()` from `autotrader/config.py`.

**Deliberately NOT reused:**
- `SimBroker`/`TradeEngine` as the venue — SimBroker overwrites `avg_price` with last fill (no weighted avg), reports `total_assets == cash` (no equity curve, risk checks can't fire), has no bar/clock concept. A purpose-built bar loop over the pure functions is smaller and more faithful. SimBroker stays untouched as the order-lifecycle test double.
- `risk_core.evaluate` — its checks are live-ops machinery (`RISK_ALLOWED_SYMBOLS` would block backtest tickers; daily-loss GATE/HALT is an ops control, not the strategy). `size_position` already applies max_position_qty/max_order_notional/max_gross_exposure in risk mode. `# ponytail:` comment marks the seam; add halt simulation if ever wanted.
- `breakout_reference.BreakoutReference` — needs a broker; the engine computes the identical rolling max directly.

**Constraints honored:** no SDK in backtest modules (and they get **added to the explicit module list** in `tests/test_no_sdk_in_core.py`); stdlib only (urllib, csv, statistics, zoneinfo — repo has no `requests`, avoids pandas in core); env-var config; CLI convention `python -m autotrader.<module>`; flat `tests/test_*.py` with module-level `_helper()` factories, `tmp_path`, `pytest.approx`, invariant-pinning docstrings.

**Massive API (verified from docs):** `GET /v2/aggs/ticker/{T}/range/{multiplier}/{timespan}/{from}/{to}?adjusted=true&sort=asc&limit=50000`; results `o/h/l/c/v/t` (unix ms); pagination via `next_url` (auth header must be re-attached); `Authorization: Bearer` header (key never in URL); 429 rate limiting (free tier ~5 req/min). Massive tickers bare (`AAPL`); production symbols `US.AAPL` → normalize.

## Architecture — new package `autotrader/backtest/`

### 1. `data.py` — Massive client + cache + hygiene
- `Bar` frozen dataclass: `ts` (unix ms), `open/high/low/close/volume`; `day()` → ET calendar date via `zoneinfo("America/New_York")`.
- `MassiveError(RuntimeError)`; `normalize_ticker("US.AAPL") -> "AAPL"` (uppercase, prefix strip).
- `MassiveClient(api_key, base_url="https://api.massive.com", opener=urlopen, sleep=time.sleep)` — injectable transport/sleep for tests. `aggs(ticker, multiplier, timespan, frm, to, adjusted=True) -> list[Bar]`: Bearer header, follow `next_url` until absent, on 429 sleep+retry (max 5 → MassiveError). Hygiene after merging pages: sort by ts, dedupe ts (keep first), drop bars with any o/h/l/c ≤ 0 or high < low. **No gap-filling** — a missing session is simply not evaluated (production fail-safe parity).
- `load_bars(client, cache_dir, ticker, multiplier, timespan, frm, to, adjusted=True, use_cache=True)` — one JSON file per exact request key: `{cache_dir}/{TICKER}_{multiplier}{timespan}_{frm}_{to}_adj{0|1}.json` holding the *cleaned* bars. Cache hit = zero network (fully cached run works keyless). Default dir `~/.autotrader/backtest_cache/` (home-dir convention like `~/.autotrader.db`; repo-local would duplicate per worktree). `# ponytail: whole-range cache key; re-shard per month if API quota ever hurts.`
- Missing `MASSIVE_API_KEY` → `MassiveError` naming `config/secure.config`.

### 2. `engine.py` — bar-walking simulator (the correctness core)
- `BacktestConfig` frozen: symbols, start/end dates, multiplier/timespan, cash, commission_per_order, commission_per_share, slippage_bps, lookback, stop_loss_pct, take_profit_pct, trailing_stop_pct (PERCENT, 0=off), confidence.
- Backtest-local `Fill` (ts, symbol, side, qty, price, commission, reason) — production `domain.Fill` is broker-shaped.
- `BacktestResult`: fills, equity_curve `[(day, total_value, cash)]`, skipped_for_cash, cfg.
- `run_backtest(bars_by_symbol, cfg, risk_cfg=None) -> BacktestResult` — takes pre-fetched bars: fully offline, trivially testable.
- **Loop:** sorted union of bar timestamps; at each ts, **exit phase for all symbols first, then entry phase** (exits free cash before entries compete), symbols in sorted order — deterministic cash contention. Per-symbol state: position, deque of completed daily highs (maxlen=lookback), forming-day high, intraday HWM. Day rollover pushes the completed high. Bars with day < start are **warmup** (feed ref_high only, no trading; CLI fetches `start − ceil(lookback·1.6 + 10)` calendar days so day one has a full window). `ref_high = max(deque)` only when `len == lookback`, else None (short history → no entry, mirrors `moomoo_broker.recent_high`).

**Intrabar fill model — exact rules, evaluated in order per bar** (`slip = slippage_bps/1e4`; BUY at px·(1+slip), SELL at px·(1−slip); commission = per_order + per_share·qty on every fill; `t = trailing_stop_pct/100`; bar `(o,h,l,c)`, avg cost `a`):
1. **Open-price exit (gaps):** `strategy.evaluate(o, position, ref_high)` → SELL ⇒ full-qty fill at `o`; reason from the Signal (production SL-before-TP ordering covers a gap breaching both).
2. **Intrabar downside stops:** fixed `P_sl = a·(1−sl)` breached if `l ≤ P_sl`; trailing (t>0): daily interval — HWM reseeds to `o` (morning-reseed parity), trigger `P_tr = o·(1−t)`; intraday — `hwm = max(hwm_prev, o)` (reseed at day rollover), trigger `hwm·(1−t)`, ratchet to `h` only **after** the fire check (pessimistic: drop before rise). Any breached → fill at the **highest** breached trigger (touched first on a falling path); reason = that trigger.
3. **Daily-interval trailing branch** (t>0, nothing fired yet): `c ≤ h·(1−t)` → fill at `h·(1−t)`, reason trailing-stop. (Reconstructs rose-then-fell-through-trail; without it, daily reseed means trailing could never fire off an intraday peak at 1d resolution.)
4. **Intrabar take-profit** (nothing fired): `h ≥ a·(1+tp)` → fill at `a·(1+tp)`. Rules 2/3 first ⇒ **stop-loss wins when SL and TP share a bar** (pessimistic).
5. **Entry** (entry phase, only if flat after exit phase — a symbol that exited this bar may NOT re-enter it): `ref_high is not None and h > ref_high` (strict) → stop-buy at `max(o, ref_high)` (gap-up fills at open). Qty via production `size_position(equity=cash + Σ_other qty·prev_close, entry_price=fill_px, signal_stop=None, confidence, cfg=risk_cfg, current_qty=0, gross_exposure=Σ qty·prev_close, fixed_qty=order_qty)`, then engine clamp `min(qty, max_position_qty, ⌊max_order_notional/px⌋, ⌊(cash−commission)/px_slipped⌋)` (covers the uncapped FIXED_DISABLED path). qty ≤ 0 → skip, `skipped_for_cash += 1`. Marks use **previous close** only.
6. **Same-bar exit after entry** (pessimistic asymmetry): entry fill `p_e`; if `l ≤ p_e·(1−sl)` → same-bar stop at `p_e·(1−sl)`; trailing arms at entry price (production stops.py:150–151) so `l ≤ p_e·(1−t)` can also fire — take the higher trigger. Same-bar **take-profit never taken** (can't verify high came after entry; would be optimistic).
7. **Entry time window** (intraday intervals only): entries only for bars starting 09:45–15:30 ET (production `EntryGate` parity); exits on any session bar. Daily interval: no window.
8. **End of data:** open positions NOT force-liquidated; final equity marks at last close; trade stats count completed round trips, open positions reported separately.

**No-look-ahead guarantee:** decisions use only completed prior days (ref_high), prior closes (marks), and the current bar's own OHLC as trigger levels (standard stop-order semantics). Current close used only for the equity curve and rule 3's path reconstruction — never for a next-bar decision. Pinned by a prefix-property test.

**Accounting:** cash −= qty·px_slipped + commission on BUY, += qty·px_slipped − commission on SELL; weighted-avg cost (flat-only entries ⇒ one lot per round trip, but the code path is correct anyway); equity appended once per calendar day at its last bar: `cash + Σ qty·close`.

### 3. `metrics.py` — pure math (stdlib `math`/`statistics`)
- `RoundTrip` frozen (symbol, entry/exit ts+price, qty, commissions, pnl, exit_reason); `pair_round_trips(fills)` (flat-only entries ⇒ strict BUY→SELL pairs per symbol).
- `max_drawdown(values) -> fraction`; `sharpe(daily_returns)` = mean/stdev·√252, rf=0, 0.0 if stdev==0 or n<2; `cagr(initial, final, days)` = `(final/initial)**(365.25/days) − 1`, 0.0 if days==0.
- `summarize(result, trips) -> dict` — portfolio: total_return, cagr, sharpe, max_drawdown, final_value, trade_count, win_rate, avg_win, avg_loss, profit_factor (None if no losses — no inf), exposure (fraction of equity-curve days with any position), total_commission, skipped_for_cash; per_symbol: same trade stats + exposure + realized pnl.

### 4. `report.py` — self-contained HTML report (the UI)
A backtest is a batch job; its UI is a report artifact, not a server. `write_html_report(result, summary, trips, cfg, path)` renders one **fully self-contained** `report.html` (inline CSS/SVG, no external assets, no JS deps — opens offline in any browser):
- Header: run config (symbols, range, interval, capital, costs, strategy knobs) + data-hygiene counts (dropped/duplicate bars).
- Portfolio metric cards (total return, CAGR, Sharpe, max drawdown, win rate, profit factor, exposure, final value) + per-symbol metrics table.
- Equity curve as an inline SVG polyline (equity + cash), with drawdown shading; axis labels from the equity-curve dates.
- Trades table (all round trips: entry/exit date+price, qty, reason, P&L, cumulative P&L), color-coded win/loss.
### 5. Dashboard integration — reuse of `dashboard/` (read-only viewer)
The existing dashboard is the project's established UI; it gains a **Backtests tab that lists and serves saved runs** — nothing more. Validated split: viewing results is read-only file serving and fits the dashboard's structural safety story (hardcoded `ALLOWED_SCRIPTS` stays untouched, no new broker access, no order path); *running* backtests from the browser does not fit (job execution would break the read-only guarantee and needs background-job plumbing just to replicate the CLI) and is deliberately excluded. `# ponytail: viewer only; the CLI is the runner.`
- `dashboard/server.py` additions:
  - `GET /api/backtests` — scan the runs root, return newest-first list of `{run_id, mtime, headline metrics from summary.json}`. **No OpenD health gate** (pure local file read; the 503 gate stays on broker-backed endpoints only).
  - `GET /backtests/<run_id>/<file>` — serve run artifacts with strict containment: resolved path must stay under the runs root, filename must be one of `report.html`, `summary.json`, `trades.csv`, `equity_curve.csv`; anything else → 404.
  - Runs root config: `DASHBOARD_BACKTEST_DIR` env / `dashboard.config` key, default `~/.autotrader/backtest_runs` (same precedence chain as existing dashboard settings).
- `dashboard/static/index.html`: a "Backtests" section listing runs with headline metrics (total return, Sharpe, max DD, trades), each linking to its `report.html` in a new tab. No polling changes — loaded on demand.

### 6. `__main__.py` — argparse CLI + CSV/JSON/HTML writing (no separate cli.py)
`main(argv=None, client_factory=None) -> int` (injectable for offline smoke tests); run as `python -m autotrader.backtest`.

| Flag | Default |
|---|---|
| `--symbols` | required; comma list; accepts `US.AAPL`/`AAPL` |
| `--from` / `--to` | required, YYYY-MM-DD; reject start > end |
| `--interval` | `1d`; regex `(\d+)([mhd])` → multiplier+timespan; else argparse error |
| `--cash` | 100000 |
| `--commission` / `--commission-per-share` | 0.0 / 0.0 |
| `--slippage-bps` | 0.0 |
| `--lookback` | env `ENTRY_BREAKOUT_LOOKBACK`, else 20 |
| `--stop-loss-pct` | env `STRATEGY_STOP_LOSS_PCT`, else 0.05 (fraction, must be > 0 — validate before `BreakoutParams` raises) |
| `--take-profit-pct` | env `STRATEGY_TAKE_PROFIT_PCT`, else 0.10 (ditto) |
| `--trailing-stop-pct` | env `RISK_TRAILING_STOP_PCT`, else 0.0 (PERCENT) |
| `--order-qty` | env `ORDER_QTY`, else 1 |
| `--out-dir` | default `~/.autotrader/backtest_runs/<UTC-timestamp>_<symbols>/` (so the dashboard finds runs); writes `report.html` (the UI), `equity_curve.csv` (date,total_value,cash), `trades.csv` (ts,date,symbol,side,qty,price,commission,reason), `summary.json` (the `summarize()` dict, for programmatic run comparison + the dashboard list) |
| `--no-save` | stdout summary only, write nothing |
| `--cache-dir` | `~/.autotrader/backtest_cache` |
| `--no-cache` | off |
| `--api-key` | env `MASSIVE_API_KEY` |

Env-default chain = the SAME env vars production reads, so a sourced `config/risk.config` gives config parity. Flow: parse → validate → normalize → warmup fetch start → `load_bars` per symbol (zero-bar symbol: warn+skip; all empty: exit 1) → `load_risk_config()` → `run_backtest` → `summarize` → print portfolio + per-symbol table → CSVs if out-dir. Confidence: env `STRATEGY_CONFIDENCE`, default 0.7.

### 7. Config & docs edits
- `config/secure.config.example`: add `MASSIVE_API_KEY=YOUR_API_KEY_HERE` + one-line comment (historical data for `python -m autotrader.backtest`).
- `config/risk.config.example`: no new knobs (backtest reuses documented ones); one comment line pointing at `docs/BACKTESTING.md`.
- `tests/test_no_sdk_in_core.py`: append `autotrader.backtest.data/engine/metrics/report` to the explicit module list.
- NEW `docs/BACKTESTING.md`: key setup, flag table, the 8 fill rules verbatim, metric interpretation, assumptions. `RUNBOOK.md` §3: one `MASSIVE_API_KEY` row.

## Tests (offline, no network; every engine test pins a numbered rule)
- `tests/test_backtest_data.py` — fake `opener` counting calls + recorded `sleep`: pagination merge w/ auth header on both pages; 429→200 retry, 6×429 → MassiveError; cache hit = zero opener calls; hygiene (dedupe keep-first, invalid drop, sort); `normalize_ticker`; missing key error names secure.config.
- `tests/test_backtest_engine.py` — hand-built bars, known answers: stop-buy at ref_high·(1+slip); gap-up fills at open; `h == ref_high` no entry (strict); no entry until lookback filled; warmup feeds ref_high but never trades; forming bar excluded from own ref_high; no re-entry while held; open-gap stop at `o`; intrabar SL/TP exact fill prices; SL+TP same bar → SL wins; trailing daily reseed (gap-down alone doesn't fire; `l ≤ o·(1−t)` does); prior-day peak not carried; rule-3 close branch; SL-vs-trailing higher trigger wins; same-bar entry→SL fires, entry→TP never; exit bar can't re-enter; multi-symbol exits-free-cash-then-entries + cash never negative; sizing parity vs direct `size_position`; fixed-mode notional clamp; **no-look-ahead prefix property** (fills ≤ D identical between `run(start,end)` and `run(start,D)`); conservation: final equity == initial + Σ pnl + unrealized (approx); intraday entry-window + hwm ratchet/reseed.
- `tests/test_backtest_metrics.py` — max_drawdown([100,120,90,130]) ≈ 0.25, monotonic → 0; sharpe constant-returns → 0.0 (no ZeroDivisionError) + known answer; cagr known answer + days=0; pair_round_trips interleaved symbols + unclosed BUY excluded; win_rate/profit_factor/avg from hand-built trips, all-winners → None; exposure known answer.
- `tests/test_backtest_cli.py` — offline smoke via `client_factory=fake`: exit 0, stdout summary, `report.html` + CSVs + `summary.json` exist w/ headers+row counts; interval parsing (`1d`, `5m`, `2x` → error); `--stop-loss-pct 0` → clean argparse error; zero-bar symbol warn+skip; all-empty → exit 1.
- `tests/test_backtest_report.py` — `write_html_report` output is self-contained (no `http://`/`src=` external refs), contains the key metric values, one SVG polyline point per equity-curve day, and one trades-table row per round trip; zero-trade result still renders (no division/empty-sequence errors).
- `tests/test_dashboard_backtests.py` — Flask test client over tmp runs root: `/api/backtests` lists runs newest-first with summary metrics and needs no OpenD; `/backtests/<id>/report.html` serves the file; path traversal (`../`) and non-whitelisted filenames → 404; empty runs root → empty list, not error.

## Edge cases pinned
Empty range/bad ticker (warn+skip, all-empty exit 1) · history < lookback after warmup (zero trades, metrics still emitted, warning) · weekend/holiday gaps (walk actual bars only, no synthetic) · splits (`adjusted=true` end-to-end; ref_high on same adjusted series; docs note production QFQ differs slightly in absolute levels) · multi-symbol contention (sorted order + exits-first = determinism contract) · open position at end (not force-sold) · `--from` before listing (entries start once lookback fills).

## Assumptions (documented in BACKTESTING.md)
- Daily bars approximate production's 5s intraday quote loop; entry-window gating isn't modelable at 1d (applied for intraday intervals). Minute bars narrow the gap.
- Multi-ticker = N independent instances of the (single-symbol) production strategy sharing one cash pool.
- Fills pessimistic wherever intrabar ordering is unknowable (SL priority, ratchet-after-check, no same-bar TP after entry).
- Adjusted prices ≈ production QFQ.

## Implementation order & verification
1. `data.py` + tests → 2. `engine.py` + tests (rules 1–8) → 3. `metrics.py` + tests → 4. `report.py` + tests → 5. `__main__.py` + CLI tests + config/docs/no-SDK-list edits → 6. dashboard Backtests tab + tests.
- `uv run pytest -q` — full suite green (existing 731 + new), `test_no_sdk_in_core.py` covering the new modules.
- Offline E2E: CLI smoke via fake client produces summary + CSVs that reconcile.
- If `MASSIVE_API_KEY` is present in the environment at run time: one real smoke run (e.g. 2 symbols, 2 years daily) to validate client + produce a sample report; otherwise documented as the one pending validation step.

## Files changed/added
- NEW `autotrader/backtest/{__init__,__main__,data,engine,metrics,report}.py`
- NEW `tests/test_backtest_{data,engine,metrics,report,cli}.py`, `tests/test_dashboard_backtests.py`
- NEW `docs/BACKTESTING.md`
- EDIT `dashboard/server.py` (+2 read-only endpoints), `dashboard/static/index.html` (Backtests section), `config/dashboard.config.example` (`backtest_dir` key)
- EDIT `config/secure.config.example`, `config/risk.config.example` (comment), `tests/test_no_sdk_in_core.py` (module list), `RUNBOOK.md` (§3 row)
