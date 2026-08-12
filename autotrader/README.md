# AutoTrader core (paper v1)

Deterministic paper-trading core for Moomoo OpenD. Architecture and rationale:
`docs/research/autotrader-architecture-research.md`. Plan: `docs/superpowers/plans/2026-06-12-autotrader-paper-v1.md`.

## Rings
- `strategies/` — stateless: (price, position) -> Signal | None. No execution.
- `risk_core.py` — deterministic gate. The ONLY approver of orders.
- `router.py` — audit-first JSONL + idempotency + per-symbol lock.
- `broker.py` / `sim_broker.py` / `moomoo_broker.py` — Broker port + adapters.
  All Moomoo-isms live in `moomoo_broker.py`; it never sends an SDK trade-unlock.
- `main.py` — readiness gate -> tick -> cancel-on-shutdown.

## Run (paper only)
1. Start & authenticate the OpenD GUI (paper account). Install via `/install-moomoo-opend`.
2. `cp config/risk.config.example config/risk.config` and review limits (human-reviewed).
3. Load the config and account, then run:
   ```bash
   set -a && source config/risk.config && set +a   # exports RISK_*
   export FUTU_ACC_ID=<your SIMULATE acc_id>        # from get_accounts.py
   export ENTRY_BREAKOUT_LOOKBACK=20                 # N-day breakout window (default)
   python -m autotrader.main
   ```
   **Internal strategy.** `STRATEGY_KIND=breakout` (default) enters only on a
   new N-day high (`price > the highest high of the last ENTRY_BREAKOUT_LOOKBACK
   completed daily bars`); `STRATEGY_KIND=pullback` enters only on a dip to the
   `ENTRY_PULLBACK_LOOKBACK`-day low inside a `REGIME_SMA`-day-SMA uptrend. If
   the daily klines can't be fetched neither kind enters — never a buy-at-open.
   Set `STRATEGY_ENABLED=false` to run the engine on webhook + rebalance signals
   only. See RUNBOOK.md for full config.

LIVE is intentionally blocked in v1 (`main()` refuses non-PAPER). Live is a later,
separately-validated phase requiring the two-key unlock.

## Test
- Offline core: `python -m pytest tests/ --ignore=tests/test_moomoo_broker_live.py`
- Live adapter (OpenD running, paper): `RUN_LIVE=1 python -m pytest tests/test_moomoo_broker_live.py`
