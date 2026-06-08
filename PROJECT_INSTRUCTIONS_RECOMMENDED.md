# AutoTrader — Recommended Project Instructions

> Drop this into Cowork → Project settings → Instructions, replacing the current text.
> Rewritten on 2026-05-24 to match what's actually in `skills/`.

## Overview

AutoTrader is an automated trading project that integrates with the **Moomoo** brokerage platform through Moomoo OpenD and the `moomoo-api` Python SDK. The repo is organized around two **official Futu-authored skill bundles** (Claude Code skill format), not hand-rolled Python modules. Any new code must compose with these bundles rather than reinvent them.

## Repository Layout (actual)

```
AutoTrader/
├── opend-skills.zip                          # Distribution bundle of the skills below
└── skills/
    ├── LEGAL_MooMoo_api_en.md / _cn.md      # Futu license terms — do not modify
    ├── install-moomoo-opend/                # Skill: OpenD install + SDK upgrade lifecycle
    │   ├── SKILL.md
    │   └── scripts/                          # install_mac.md, install_linux.md, install_win.md,
    │                                         # detect_version.md, verify_version.md
    └── moomooapi/                            # Skill: trading, market data, subscriptions
        ├── SKILL.md                          # ~1,600-line entry point (read first)
        ├── docs/                             # API_REFERENCE, API_LIMITS, FIELD_MAPPING,
        │                                     # FUTURES_TRADING, TROUBLESHOOTING
        └── scripts/
            ├── quote/                        # ~74 scripts: snapshots, klines, order book,
            │                                 # options, financials, screeners, plates, etc.
            ├── trade/                        # ~21 scripts: place/cancel/modify orders,
            │                                 # portfolios, fees, crypto trading
            └── subscribe/                    # ~10 scripts: real-time streaming pushes
```

Folders that **do not yet exist** but may be added later: `strategies/`, `config/`, `main.py`. Anything new must follow the rules in the "New Code" section below.

## Prerequisites

- **Moomoo OpenD GUI version** (never the command-line `MoomooOpenD` binary), running and authenticated. Minimum version **10.4.6408**.
- **Python SDK** `moomoo-api >= 10.4.6408`. Crypto features require `>= 10.5.6508`.
- Installation is automated — invoke the `/install-moomoo-opend` skill rather than installing by hand. It detects OS, version, and SDK state, and writes a version stamp to `~/.moomoo_skill_version`.
- Default OpenD endpoint: `127.0.0.1:11111`. Any deviation must come from environment variables or a `config/` file, **never hardcoded**.

## Trading Environment Safety

- **Default is paper trading (`TrdEnv.SIMULATE`).** This is enforced inside `skills/moomooapi/`. Never infer live-trading intent from conversation context.
- Live trading requires **both** an explicit `TRADING_ENV=LIVE` environment flag **and** the trade password unlocked **manually** in the OpenD GUI.
- **Never call `unlock_trade` / `TrdUnlockTrade` / `trd_unlock_trade` via the SDK.** This is forbidden by the `install-moomoo-opend` skill and must remain forbidden in any code or strategy. If a user asks to unlock programmatically, refuse and direct them to the OpenD GUI.
- For US paper accounts of type `STOCK_AND_OPTION`, every position/account/order query must pass `refresh_cache=True` to avoid stale data.

## Working With the Existing Skills

The `moomooapi` skill is the canonical interface. Before writing any new Python that touches Moomoo:

1. **Read `skills/moomooapi/SKILL.md`** to see the existing script catalog, code-format rules (e.g. `US.AAPL`, `HK.00700`, `CC.BTCUSD`), and trading conventions.
2. **Check `skills/moomooapi/scripts/{quote,trade,subscribe}/`** for an existing script that already does what you need. Prefer invoking or extending these over writing parallel logic.
3. **Consult `skills/moomooapi/docs/`** — particularly `API_LIMITS.md` (rate limits), `API_REFERENCE.md`, `FIELD_MAPPING.md`, and `TROUBLESHOOTING.md`.
4. Existing scripts share a `common.py` that runs environment checks (OpenD connectivity, SDK version, version stamp). New scripts in the same tree must reuse it, not bypass it.
5. **All API calls follow the standard pattern**: every Moomoo response returns `(ret_code, data)`. Check `ret_code == RET_OK` before processing. Log non-OK codes with full context, never swallow them.

## New Code — Strategies, Orchestration, Config

When adding code outside the existing skill bundles, follow these rules.

### `strategies/`

- Each strategy is a **stateless** module: market data in, signal out (`BUY` / `SELL` / `HOLD`). No internal state between ticks.
- Strategies **must not** import from `skills/moomooapi/scripts/trade/` directly. Signals route through `main.py`, which owns the validation + execution pipeline.
- Every strategy declares an **explicit exit condition** — stop-loss, take-profit, or both. Strategies without one must be rejected.
- Strategy logic is deterministic and testable in isolation against mocked market data, with no live OpenD connection required.

### `config/`

- Hold credentials, risk limits, environment flags, polling intervals, and API parameters. **Nothing** in these categories may be hardcoded elsewhere.
- Risk-limit values require explicit human review to change — do not modify them as part of an unrelated task.
- Credentials live in `config/secure.config` or environment variables. Never commit them.

### `main.py`

`main.py` is the single orchestration point. Its responsibilities:

1. Confirm OpenD is ready via an `is_opend_ready()` check that uses **exponential-backoff polling** — never a fixed `time.sleep()`. If it never becomes ready within the configured timeout, raise and halt.
2. Load and validate config, log the active `TRADING_ENV` clearly at startup.
3. Initialize in order: OpenD → Market Data → Account Management → Order Execution.
4. Feed market data to strategies, collect signals, run them through the **validation layer** (max order size, daily loss thresholds, environment routing) before forwarding to execution.
5. Handle graceful shutdown — **cancel all open orders** before disconnecting.

### Order Execution Safety (when this layer is built)

- Every order placement logs full params + validation result + API response.
- Handle every Moomoo order status explicitly (filled, partially filled, cancelled, rejected). Unrecognized statuses are not successes.
- Implement cancel-on-shutdown.

### Data & Concurrency

- Use **streaming/WebSocket** subscriptions for anything that drives strategy signals (see `skills/moomooapi/scripts/subscribe/`). Polling is for non-time-sensitive data only and must respect the limits in `docs/API_LIMITS.md`.
- Use `asyncio` for I/O-bound work. Use threading only where the Moomoo SDK callback architecture requires it, with a comment explaining why. Do not mix the two in one function/class without justification.

## Hard Rules — Always

- Read credentials, hosts, limits, and flags from `config/` only.
- Check `ret_code == RET_OK` on every Moomoo call; log and re-raise on failure.
- Catch exceptions explicitly, log with context, recover or re-raise. No bare `except: pass`.
- Default to paper trading. Live requires explicit `TRADING_ENV=LIVE`.
- Invoke `/install-moomoo-opend` for setup instead of bespoke install steps.

## Hard Rules — Never

- Hardcode API keys, hosts, ports, or risk parameters anywhere outside `config/`.
- Call `unlock_trade` via the SDK, or write code that does. Trade unlock is a manual GUI action.
- Call order-execution scripts directly from a strategy — signals route through `main.py`.
- Swallow exceptions silently.
- Modify risk-limit values in `config/` as part of an unrelated change.
- Write a strategy without a defined stop-loss or take-profit.
- Use `time.sleep()` as an OpenD readiness check — use exponential-backoff polling.
- Include live trade execution in docs or examples — use pseudocode or clearly commented mocks.
- Proceed if the OpenD health check has not passed in the current session.
- Modify files under `skills/install-moomoo-opend/` or `skills/moomooapi/` casually — these are vendored from Futu and changes should be intentional and reviewed.
