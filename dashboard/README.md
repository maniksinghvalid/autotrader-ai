# AutoTrader — Operations Dashboard

A read-only, live-refreshing web view of your Moomoo **paper trading** account:
account summary, positions, open orders, and today's fills. It also includes a
**Backtests** tab that lists and links to saved `python -m autotrader.backtest`
runs (see [docs/BACKTESTING.md](../docs/BACKTESTING.md)) — pure local file
serving, no OpenD needed for that tab.

It is a thin presentation layer. All Moomoo access is delegated to the vendored
`skills/moomooapi` scripts, which run their own environment checks, set
`refresh_cache=True`, and enforce the `ret_code == RET_OK` pattern. The
dashboard never places, modifies, cancels, or unlocks trades.

## Prerequisites

1. **Moomoo OpenD GUI** running and authenticated (min version `10.4.6408`).
   This is a native app, **not** a pip package — install it via the
   `/install-moomoo-opend` skill (it also writes the `~/.moomoo_skill_version`
   stamp). Nothing below replaces this step.
2. **Python deps** — both Flask and the `moomoo-api` SDK, pinned in
   `dashboard/requirements.txt`. The SDK must share the dashboard's environment
   because `server.py` invokes the vendored scripts with `sys.executable`.

   ```bash
   pip install -r dashboard/requirements.txt
   ```

   or with [uv](https://docs.astral.sh/uv/) (see [Run](#run) for the full recipe).

## Configure (optional)

OpenD connection + credentials come from environment variables (read by the
vendored `common.get_config()`), never from the dashboard:

```bash
export FUTU_OPEND_HOST=127.0.0.1     # default
export FUTU_OPEND_PORT=11111         # default
# export FUTU_ACC_ID=123456          # optional: pin a specific account
```

Presentation settings (port, refresh cadence) are optional and non-secret.
Copy the example and edit if you want to change defaults:

```bash
cp config/dashboard.config.example config/dashboard.config
```

| Setting              | Default | Notes |
|----------------------|---------|-------|
| `web_host`           | 127.0.0.1 | Bind address for the dashboard |
| `web_port`           | 8787    | Dashboard port |
| `refresh_seconds`    | 30      | Keep ≥ 20s to respect Moomoo's 10-refresh-per-30s limit |
| `opend_ready_timeout`| 30      | Startup OpenD readiness poll budget |
| `preferred_market`   | US      | Market chosen when an account has several |
| `backtest_dir`       | `~/.autotrader/backtest_runs` | Root the Backtests tab scans; matches the backtest CLI's default `--out-dir` |

Env overrides: `DASHBOARD_PORT`, `DASHBOARD_REFRESH_SECONDS`,
`DASHBOARD_OPEND_TIMEOUT`, `DASHBOARD_PREFERRED_MARKET`, `DASHBOARD_HOST`,
`DASHBOARD_BACKTEST_DIR`.

## Run

```bash
python dashboard/server.py
```

### With uv

`uv` builds an isolated environment from the same `requirements.txt`. Because
`server.py` shells out to the vendored scripts via `sys.executable`, running the
server under uv makes both Flask **and** `moomoo-api` available to those
subprocesses automatically — no interpreter mismatch.

```bash
# Reproducible (creates ./.venv) — recommended:
uv venv
uv pip install -r dashboard/requirements.txt
uv run python dashboard/server.py

# Or ephemeral, no venv directory:
uv run --with-requirements dashboard/requirements.txt python dashboard/server.py
```

uv installs the **Python SDK only**; the OpenD GUI (Prerequisite 1) must already
be running and authenticated. The `~/.moomoo_skill_version` stamp is written by
`/install-moomoo-opend`, not by uv — without it `common.py` prints a one-line,
non-blocking version warning, but the dashboard still serves.

On startup the server runs an `is_opend_ready()` check (exponential-backoff
polling — no fixed sleeps). Then open:

```
http://127.0.0.1:8787/
```

The dashboard auto-detects your paper (`SIMULATE`) accounts, lets you switch
between them, and refreshes on the configured cadence. A "paper" badge is shown
at all times; it only ever reads the live account if you explicitly export
`TRADING_ENV=LIVE` (and even then it remains read-only).

## Safety model

- **Paper by default.** `TrdEnv.SIMULATE` unless `TRADING_ENV=LIVE` is explicit.
- **Read-only allow-list.** The backend can only invoke `get_accounts`,
  `get_portfolio`, `get_orders`, and `get_order_fill_list`. No order or unlock
  scripts are reachable.
- **OpenD gate.** Data endpoints return `503` until OpenD passes the health
  check, instead of proceeding silently.
- **No hardcoded secrets, hosts, ports, or risk limits.**

## Files

```
dashboard/
├── server.py          # Flask backend (read-only data layer + OpenD gate)
├── opend_ready.py     # is_opend_ready() exponential-backoff readiness check
├── requirements.txt
├── README.md
└── static/
    └── index.html     # Single-file UI (KPIs, positions, orders, fills)
config/
└── dashboard.config.example
```
