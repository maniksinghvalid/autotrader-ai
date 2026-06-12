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
3. Load the config and account, set an entry price, then run one tick:
   ```bash
   set -a && source config/risk.config && set +a   # exports RISK_*
   export FUTU_ACC_ID=<your SIMULATE acc_id>        # from get_accounts.py
   export ENTRY_PRICE=<price>                        # see warning below
   python -m autotrader.main
   ```
   **Warning:** v1 runs a single tick and `ENTRY_PRICE` defaults to `0`. With an
   unset/zero entry, the threshold `price >= entry` is always true, so the bot
   buys at market immediately (the deliberate "place one order" demo). Set
   `ENTRY_PRICE` to the level you actually want to trigger entry.

LIVE is intentionally blocked in v1 (`main()` refuses non-PAPER). Live is a later,
separately-validated phase requiring the two-key unlock.

## Test
- Offline core: `python -m pytest tests/ --ignore=tests/test_moomoo_broker_live.py`
- Live adapter (OpenD running, paper): `RUN_LIVE=1 python -m pytest tests/test_moomoo_broker_live.py`
