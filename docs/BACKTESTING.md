# Backtesting

`python -m autotrader.backtest` runs the **production breakout strategy** —
the same `autotrader.strategies.breakout` / `.exits` / `.sizing` code
`autotrader.main` drives live — against historical OHLCV bars from the
[Massive API](https://massive.com/docs) (formerly Polygon.io). No SDK, no
broker, no OpenD connection; fully offline once data is cached.

## 1. Set up the Massive API key

Copy `config/secure.config.example` to `config/secure.config` (gitignored)
and fill in:

```
MASSIVE_API_KEY=YOUR_API_KEY_HERE
```

Then source it before running:

```bash
set -a && source config/secure.config && set +a
```

A key is only needed on a **cache miss** — a fully cached run (same
symbols/dates/interval already fetched once) needs no key at all.

## 2. Run a backtest

```bash
python -m autotrader.backtest \
  --symbols AAPL,MSFT \
  --from 2024-01-01 --to 2026-01-01 \
  --interval 1d \
  --cash 100000 \
  --commission 1.0 --slippage-bps 5
```

This prints a summary table to stdout and writes a run directory (default
`~/.autotrader/backtest_runs/<UTC-timestamp>_<symbols>/`, override with
`--out-dir`) containing:

- `report.html` — a self-contained report (metric cards, equity curve, trade
  log). Opens standalone in any browser, no server needed.
- `equity_curve.csv` — `date,total_value,cash`, one row per trading day.
- `trades.csv` — the raw fill ledger: `ts,date,symbol,side,qty,price,commission,reason`.
- `summary.json` — the full metrics dict (portfolio + per-symbol), for
  programmatic comparison across runs.

Pass `--no-save` to print the summary only.

## 3. Flags

Strategy knobs default from the **same env vars** `autotrader.main` reads, so
sourcing `config/risk.config` before running gives config parity with
production (same lookback, same stop/target, same trailing-stop percent).

| Flag | Default | Env fallback |
|---|---|---|
| `--symbols` | *(required)* | — |
| `--from` / `--to` | *(required, `YYYY-MM-DD`)* | — |
| `--interval` | `1d` | — |
| `--cash` | `100000` | — |
| `--commission` | `0.0` (per order) | — |
| `--commission-per-share` | `0.0` | — |
| `--slippage-bps` | `0.0` | — |
| `--lookback` | `20` | `ENTRY_BREAKOUT_LOOKBACK` |
| `--stop-loss-pct` | `0.05` | `STRATEGY_STOP_LOSS_PCT` |
| `--take-profit-pct` | `0.10` | `STRATEGY_TAKE_PROFIT_PCT` |
| `--trailing-stop-pct` | `0.0` (percent, e.g. `5.0` = 5%) | `RISK_TRAILING_STOP_PCT` |
| `--strategy` | `breakout` (`breakout` \| `breakout_regime` \| `pullback`) | `STRATEGY_KIND` |
| `--regime-sma` | `200` | `REGIME_SMA` |
| `--pullback-lookback` | `10` | `ENTRY_PULLBACK_LOOKBACK` |
| `--pullback-depth-pct` | `0.0` (0 = M-day-low trigger) | — |
| `--confidence` | `0.7` | `STRATEGY_CONFIDENCE` |
| `--order-qty` | `1` | `ORDER_QTY` |
| `--out-dir` | `~/.autotrader/backtest_runs/<timestamp>_<symbols>/` | — |
| `--no-save` | off | — |
| `--cache-dir` | `~/.autotrader/backtest_cache` | — |
| `--no-cache` | off | — |
| `--api-key` | — | `MASSIVE_API_KEY` |

`--symbols` accepts either bare tickers (`AAPL`) or production-style codes
(`US.AAPL`) — both normalize to the same bare ticker Massive expects.

## 4. How fills are modeled (no look-ahead)

The engine walks bars chronologically. Every decision uses only bars
**strictly before** the current one (the rolling N-day reference high, the
previous close used for position sizing) plus the current bar's own OHLC as
trigger levels — standard stop-order semantics, never a peek at a later bar.

Per bar, in order:

1. **Open-price exit (gaps).** If the strategy's exit rule (stop-loss checked
   before take-profit) fires at the bar's open, the position exits fully at
   the open price. Done with this bar's exits.
2. **Intrabar downside stops.** A fixed stop-loss (`avg_price * (1 - stop_loss_pct)`)
   and, if trailing is enabled, a trailing stop are checked against the bar's
   low. The trailing trigger **reseeds every day to that day's own open**
   (live parity — an overnight gap never fires it by itself) and ratchets up
   to the day's high only **after** the fire check (pessimistic: a drop is
   assumed to precede a rise within the bar). If either is breached, the
   position exits at the **higher** of the breached levels (touched first on
   a falling path).
3. **Daily-interval trailing branch.** At `1d` resolution only: if the close
   is at or below `high * (1 - trailing_pct)`, the position exits there. This
   reconstructs a "rose then fell through the trail" day that the daily
   reseed would otherwise miss entirely.
4. **Intrabar take-profit.** Checked last — rules 2/3 already returned if
   they fired, so **a stop-loss always wins over a take-profit reachable in
   the same bar** (pessimistic).
5. **Entry.** A flat symbol enters when the bar's high strictly exceeds the
   rolling N-day reference high, filling at `max(open, reference_high)` (a
   gap-up fills at the open; otherwise at the trigger level it crossed).
   Quantity comes from the production `size_position` sizer, then clamped to
   `max_order_notional` / `max_position_qty` / available cash (covers the
   otherwise-uncapped fixed-quantity sizing path). Marks for portfolio
   equity/exposure use the **previous** close only.
6. **Same-bar exit after entry.** An entry can be immediately stopped out
   (fixed or trailing) within its own bar if the low breaches the trigger —
   but a same-bar **take-profit is never taken** (there's no way to know the
   high occurred after the entry, so assuming it did would be optimistic).
7. **Entry time window.** For intraday intervals only, entries are restricted
   to 09:45–15:30 ET (production `EntryGate` parity). Daily bars have no
   window; exits are never gated by time.
8. **End of data.** Open positions are not force-liquidated — final equity
   marks them at the last close, and trade statistics count only completed
   round trips.

Strategy variants (`--strategy` in code, `BacktestConfig.strategy`) add four
rules. `breakout_regime` is the plain breakout plus Rule 10; `pullback` is a
mean-reversion long governed by Rules 9–12:

9. **Pullback trigger level.** `T = min(last M completed daily lows)` when
   `pullback_depth_pct` is 0, else `T = SMA_R · (1 − depth)`. Computed from
   completed data only; insufficient history → no entry.
10. **Regime gate.** Entry allowed only if the previous *completed* day's
    close is above the R-day SMA of completed closes. The current bar is
    never consulted for the gate. (For `breakout_regime`, this gate is ANDed
    in front of the Rule-5 trigger.)
11. **Limit-buy fill.** If the bar's low reaches `T`, the entry fills at
    `min(open, T)` — a resting buy-limit fills at the open when the open
    gaps below the limit. Slippage applies on top. Low above `T` → no entry.
    The Rule-7 entry window still applies on intraday timespans.
12. **Same-bar exit after a pullback entry.** Identical to Rule 6: a same-bar
    stop-loss/trailing stop may fire, a same-bar take-profit never does.

## 5. Interpreting results

- **Total return / CAGR** — portfolio value change over the run, annualized.
- **Sharpe** — annualized (√252), risk-free rate 0, computed on daily
  portfolio returns.
- **Max drawdown** — largest peak-to-trough decline in portfolio value.
- **Win rate / profit factor / avg win / avg loss** — from paired round trips
  (entry fill → matching full-position exit fill). An unclosed position at
  the end of the run isn't counted as a trade.
- **Exposure** — fraction of trading days with any open position (portfolio)
  or with that specific symbol held (per-symbol).
- **Skipped (cash)** — entries the sizer wanted to take but cash/notional
  caps reduced to zero.

## 6. Assumptions & limitations

- **Daily bars approximate the live 5-second intraday loop.** Production
  evaluates the strategy continuously against streaming quotes; a daily
  backtest only sees one bar per session. The entry-window gate (09:45–15:30
  ET) isn't meaningfully modelable at daily resolution and is skipped there;
  it's enforced for intraday intervals. Minute bars narrow this gap.
- **Multi-ticker runs generalize the strategy**, which trades one symbol at a
  time in production, to N independent instances sharing one cash pool. Same
  symbol never re-enters mid-position; different symbols compete for cash
  deterministically (exits free cash before entries spend it, symbols
  processed in sorted order).
- **Adjusted prices** (splits/dividends) are used end-to-end, matching
  production's QFQ-adjusted klines in spirit; the exact adjustment
  methodology differs by vendor, so absolute price levels can differ
  slightly from what a live session would have seen.
- **Fills are deliberately pessimistic** wherever the true intrabar path is
  unknowable: stop-loss beats a same-bar take-profit, the trailing ratchet
  is checked before it's raised, a same-bar take-profit after entry is never
  assumed. This means backtest results are a conservative estimate, not an
  optimistic one.
- **No gap-filling.** A missing session in the data is simply not evaluated,
  the same fail-safe production uses when a kline fetch comes back empty.

## 7. Parameter/variant sweep (`python -m autotrader.backtest.sweep`)

Sweeps 486 configurations across three strategy families — plain breakout,
regime-filtered breakout (entries only above a 100/200-day SMA), and pullback
(mean-reversion dip-buying within an SMA uptrend) — with **out-of-sample
validation**:

```bash
python -m autotrader.backtest.sweep            # defaults: 7 portfolio tickers,
                                               # train 2023-01-01..2024-12-31,
                                               # validate 2025-01-01..today
```

Selection is a lexicographic constraint ranking, not a composite score:

1. Train filter: ≥ `--min-train-trades` (30) trades AND max drawdown ≤
   `--max-dd` (5%). Excluded rows keep an `excluded_reason` in `train_grid.csv`.
2. Train ranking by win rate (tie: Sharpe); top `--top-k` (10) **per family**
   advance (per-family cap = multiple-testing control).
3. Candidates re-run on the validation window; the constraints apply **on
   validation too** (≥ `--min-val-trades` (8) trades, max drawdown ≤ cap).
4. Final ranking by **validation** win rate. Every row reports `gap`
   (train − validation win rate — a large gap means the config was curve-fit)
   and validation profit factor / avg win / avg loss, because win rate alone
   is gameable: a tiny take-profit against a wide stop can show a high win
   rate while losing money (profit factor < 1).
5. The best validated config gets a full run of record over the validation
   window only, in `~/.autotrader/backtest_runs/` — visible in the dashboard.

If nothing meets the constraints out-of-sample, the output is the nearest-miss
frontier with each row's violated constraint named. **That is the honest
deliverable** — a target win rate cannot be manufactured by tuning harder
without destroying the number's meaning.

## 8. Distinguishing what a backtest run tells you

- **Strategy logic validation** — do the entry/exit/sizing rules behave as
  designed? Covered by `tests/test_backtest_engine.py`, which pins each
  numbered rule above against hand-built bars with known answers.
- **Backtest results** — a specific run's numbers (this symbol set, this
  date range, these costs) are a sample, not a guarantee; check exposure and
  trade count before trusting a Sharpe ratio computed from a handful of
  trades.
- **Data/API issues** — a warning naming a symbol ("no data returned —
  skipping") means Massive returned nothing for that request, not that the
  strategy found no setups. Check the ticker and date range.
- **Assumptions and limitations** — see §6. A backtest cannot fully replicate
  live execution; treat it as directional evidence, not a live-P&L forecast.
