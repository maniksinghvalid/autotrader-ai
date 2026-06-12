# AutoTrader Final Roadmap — Implemented Core × Gemini Design Synthesis

**Date:** 2026-06-12 (updated) · **Branch:** `feat/autotrader-paper-v1` (26 commits)
**Inputs:** `docs/superpowers/plans/2026-06-12-autotrader-paper-v1.md` (implemented, live-verified) · `plan/gemini-code-moomoo-bot-design.md` (proposed) · `docs/research/autotrader-architecture-research.md` (governing architecture)
**Execution plans derived from this roadmap:** `2026-06-12-autotrader-phase-2a-foundation.md` (Phase 2a — **done**, 8/8 acceptance criteria green)

This document is the reconciliation the two plans needed: it maps every element of the
Gemini design onto the implemented three-ring architecture, adopts its best parts,
omits the parts the vetting findings (C1–C9, H1–H8 in the implemented plan) ruled out,
and lays out the full path from what exists today to the final system.

---

## 1. Final System Architecture

The Gemini "dual-engine" concept **survives** — but both engines sit *above* the
implemented deterministic core, never beside it. Nothing reaches the broker except
through `risk_core.evaluate()` → `OrderRouter.submit()`.

```
            ┌─────────────────────────┐   ┌──────────────────────────────┐
            │  EVENT-DRIVEN ENGINE    │   │  STATE RECONCILIATION ENGINE │
            │  (Gemini engine #1,     │   │  (Gemini engine #2, adapted) │
            │   adapted)              │   │  EST lifecycle scheduler:    │
            │  • subscribe push_quote/│   │  08:30 ground-truth sync     │
            │    push_kline (local)   │   │  09:45 entry window open     │
            │  • Pydantic-validated   │   │  15:30 risk sweep (ex-div)   │
            │    external signals,    │   │  16:15 cancel-all + commit   │
            │    localhost-only       │   │  + watchdog heartbeat        │
            └───────────┬─────────────┘   └─────────────┬────────────────┘
                        │  Signal (data value)          │ reconcile / halt
                        ▼                               ▼
            ┌───────────────────────────────────────────────────────────┐
            │            DETERMINISTIC CORE (BUILT, LIVE-VERIFIED)      │
            │  strategies/* → risk_core.evaluate() → OrderRouter        │
            │  (stateless)    (sole approver)        (audit-first,      │
            │                                         idempotent,       │
            │                                         per-symbol lock)  │
            └───────────────────────────┬───────────────────────────────┘
                                        ▼
            ┌───────────────────────────────────────────────────────────┐
            │  Broker port: MoomooBroker (SDK confined) | SimBroker     │
            │  → OpenD 127.0.0.1:11111 → Moomoo                         │
            └───────────────────────────────────────────────────────────┘
            Persistence: JSONL audit journal (immutable, written FIRST)
                         + SQLite WAL projection (replaces state.json)
```

---

## 2. Disposition of Every Gemini Element

### Adopted (as-is or adapted)

| Gemini element | Disposition | Where it lands |
|---|---|---|
| **Dual-engine architecture** (event-driven + reconciliation) | **Adapt** — event engine becomes the local subscribe-driven loop; reconciliation becomes scheduled jobs that read broker ground truth and feed the existing `reconcile_fills`/`get_account` | Phase 2 |
| **EST chronological lifecycle** (08:30 / 09:45 / 15:30 / 16:15) | **Adopt** — as deterministic, readiness-gated scheduled jobs; each job re-runs `is_opend_ready()` and soft-halts on failure instead of trading blind | Phase 2 |
| **Pydantic signal validation layer** (`RoutineSignalPayload`, `SignalChange`, `Catalyst`) | **Adopt** — the schema is good; ingress is localhost-only (file-drop or 127.0.0.1-bound endpoint), and validated payloads normalize into `domain.Signal` before the confidence filter + risk core | Phase 2 |
| **Native Moomoo trailing stops** (`OrderType.TRAILING_STOP`, 5%) | **Adopt** — broker-resting stops are independently required by research R5 (stops must survive an OpenD outage) | Phase 2 |
| **Pre-market ground-truth sync** ("overwrite local drift with broker positions") | **Adopt** — direction of trust is correct: broker is source of truth, local store is a projection. Implemented via `reconcile_fills` + `position_list_query(refresh_cache=True)` into SQLite | Phase 2 |
| **Anti-Martingale pyramiding** (100% → 50% → 25% tranches at +10%/+20%) | **Adopt** — as a *stateless* strategy module; tranche level derived from broker positions + SQLite projection, never internal state; every order through the risk core | Phase 3 |
| **Unified-stop consolidation** (cancel old stop → reissue across full position) | **Adopt** — with Gemini's own fail-safe §7.1 enforced: wait for cancel **confirmation** before placing the replacement stop (maps to implemented `OrderState` machine; UNKNOWN is never success) | Phase 3 |
| **50/200 options matrix** (BTC at −50% premium TP / 3× premium SL) | **Adopt** — as a stateless covered-call strategy; targets stored per contract in SQLite; exits routed through risk core | Phase 4 |
| **Dynamic lot matching** (⌊shares/100⌋ − active short calls) | **Adopt** — computed from broker ground truth at decision time, guaranteeing covered-only (never naked) | Phase 4 |
| **0.20-delta strike finder** (option chain scan) | **Adopt** — composes the vendored `get_option_expiration_date` / `get_option_chain` / `get_option_volatility` quote scripts | Phase 4 |
| **Ex-dividend stop adjustment** (lower stops by dividend amount pre-market) | **Adopt** — as part of the 08:30 job; prevents false stop-outs on the ex-div gap | Phase 4 |
| **Cash buffer for buy-to-close** (§7.2) | **Adopt** — new `RiskConfig.reserve_cash`; risk core rejects orders that would dip into the reserve | Phase 4 |
| **state.json position/tranche schema** (the *fields*) | **Adapt** — the data model (tranche level, base qty, per-contract TP/SL targets) becomes SQLite tables; JSON file as a store is rejected (below) | Phases 2–4 |

### Omitted (with the finding that rules each out)

| Gemini element | Why omitted |
|---|---|
| `unlock_trade(self.pin)` via SDK + `MOOMOO_TRADE_PIN` in `.env` | **C1** — CLAUDE.md hard rule; trade unlock is a manual OpenD GUI action (research R4 two-key rule). |
| ngrok / internet-exposed `/webhook/sweep` | **H1** — internet-reachable path to an *unauthenticated* OpenD socket is research R1/R2 rebuilt as a feature. External signals enter via localhost-only ingress. |
| Orders placed off `mock_local_state` | **C2** — real orders from fabricated data; replaced by broker-ground-truth lookups. |
| Direct `OpenSecTradeContext` / `from moomoo import *` in app code | **C3** — SDK stays confined to `MoomooBroker`; everything composes the vendored skills via `common.py`. |
| Signal → `place_order` with no validation layer | **C4** — the deterministic risk core is non-negotiable; both engines feed it, neither bypasses it. |
| `moomoo==6.3.2` | **C5** — wrong package and ~4 majors stale; we run `moomoo-api` 10.07.6708 (≥10.4.6408). |
| No `is_opend_ready()` gate | **C6** — every entrypoint and scheduled job gates on the exponential-backoff readiness check (built). |
| Missing `refresh_cache=True` | **C7** — enforced in `MoomooBroker` on all account/position/order/fill queries (built). |
| Secrets committed (`.env` with PIN + webhook secret) | **C8** — secrets live in env vars / `config/secure.config`, git-ignored (built: `.gitignore` blocks them). |
| Swallowed `ret_code`s | **C9** — adapter maps non-OK to REJECTED/UNKNOWN/raise; fail-closed staleness on query failure (built). |
| `state.json` as source of truth | **D2** — two-writer race + drift; replaced by JSONL audit journal (immutable) + SQLite WAL projection, broker as ground truth. |
| FastAPI **in the order path** | **H8/H1** — the trade loop is a plain deterministic Python process. A FastAPI app may return later as a *read-only* localhost status API (the dashboard already fills this role). |
| `BackgroundTasks` fire-and-forget execution | **H5** — replaced by the synchronous risk-gated tick with idempotent `client_order_id`. |
| Covered-call writing in v1 off mock state | **H6** — options deferred to Phase 4, after paper validation, computed from real positions. |
| MARKET orders carrying a price / options at `last_price` limit | **H7** — order-type handling normalized in the adapter; option entries will price off bid/ask, not last. |

---

## 3. DONE — Phase 0 + Phase 1 (built, reviewed, live-verified)

All on `feat/autotrader-paper-v1`; **56 tests passing** (54 offline + 2 live), zero skips, with SDK 10.07.6708 installed and OpenD running.

- **R19 patch** — MASTER-account rejection added to `place_crypto_order.py` + `modify_order.py` (tests now run live and pass).
- **`autotrader/domain.py`** — frozen value objects (`Signal`, `OrderRequest`, `OrderAck` w/ immutable raw, `Fill`, `Position`, `AccountSnapshot`), `OrderState` (UNKNOWN ≠ success), `BrokerError` taxonomy.
- **`autotrader/config.py`** — frozen `RiskConfig` from `RISK_*` env; paper default; human-reviewed limits.
- **`autotrader/risk_core.py`** — pure `evaluate()`: env routing, stale-refusal, daily-loss halt, allow-list, non-finite-price rejection, notional clamp, **long-only** position cap, gross-exposure cap. 13 tests incl. security hardenings (case-canonicalization, NaN, short-block).
- **`autotrader/router.py`** — audit-line-**before**-order (halts if unwritable), idempotent sha1 `client_order_id`, per-symbol lock, exception→UNKNOWN (audited, cached).
- **`autotrader/broker.py` / `sim_broker.py` / `moomoo_broker.py`** — Broker port; deterministic SimBroker; MoomooBroker composing `common.py` factories, `refresh_cache=True` everywhere, fail-closed staleness, no SDK leak (subprocess-enforced by `test_no_sdk_in_core.py`).
- **`autotrader/strategies/threshold.py`** — stateless, explicit stop+target enforced at construction, inclusive boundaries pinned by tests.
- **`autotrader/main.py`** — `TradeEngine.tick()` pipeline + `main()` (refuses non-PAPER, `is_opend_ready()` gate, cancel-on-shutdown, broker always closed).
- **Phase-1 exit gate passed live (2026-06-12):** risk core first *rejected* on real exposure (`15097.34 > 10000` cap); with a sized cap, order `808174` (US.AAPL BUY 1, SIMULATE acct `1727266`) placed with audit `intent` → `ack` ordering verified, then `CANCELLED_ALL`, 0 filled, account clean.

### Phase 2a — Foundation (built this iteration; **70 offline tests passing**, 8/8 acceptance criteria green)

Per `docs/superpowers/plans/2026-06-12-autotrader-phase-2a-foundation.md`:
- **`autotrader/rate_limiter.py`** — thread-safe token bucket; two instances in `MoomooBroker` (15 orders/30 s, 10 refresh/30 s). A refresh-query rate-limit degrades to a *stale* snapshot, preserving fail-closed staleness.
- **`autotrader/db.py`** — SQLite **WAL projection**, 6 tables (`signals`, `trades` UNIQUE `client_order_id`, `fills` PK `fill_id`, `positions`, `performance`, `halts`) with idempotent/upsert semantics; injected as an optional `DB` into `TradeEngine`, wired through `main()` via `AUTOTRADER_DB_PATH`.
- **Carry-over fixes:** SELL exits sized to `position.qty` (not `order_qty`); `reconcile_fills(since=…)` forwards `begin_time`; per-order cancel logging. SDK confinement still subprocess-enforced.

---

## 4. TO DO — Path to the Final State

### Phase 2 — Paper validation infrastructure (the two Gemini engines, made safe)
*Goal: run multi-week unattended paper sessions with zero safety violations.*

Split into three execution plans: **2a Foundation (DONE)** · 2b Engines · 2c Signals.

- [x] **SQLite WAL projection** — `signals`, `trades` (`client_order_id` UNIQUE), `fills` (`fill_id` PK), `positions`, `performance`, `halts`. **(Phase 2a — done.)** Dashboard read-wiring is a 2b task.
- [x] **Carry-over fixes:** SELL exits sized to `position.qty`; `reconcile_fills(since=…)` filter; per-order cancel logging; rate-limit token bucket (15 orders/30 s, refresh 10/30 s). **(Phase 2a — done.)**
- [ ] **Continuous loop (2b):** replace the single tick with a subscribe-driven loop (`push_quote`/`push_kline` via the vendored `subscribe/` scripts) feeding `TradeEngine`.
- [ ] **EST lifecycle scheduler (2b)** (Gemini's four crons, readiness-gated): 08:30 ground-truth sync (positions + `reconcile_fills` → SQLite, dedupe by `fill_id`); 09:45 entry-window enable; 15:30 risk sweep; 16:15 `cancel_all` + state commit + halt.
- [ ] **Watchdog (2b)** — heartbeat (`get_global_state`), soft-halt on loss, backoff reconnect, **full reconcile before resuming** (research E8).
- [ ] **Pydantic external-signal ingress (2c)** — Gemini's `RoutineSignalPayload` schema, localhost-only, normalized to `domain.Signal`, through confidence filter + risk core.
- [ ] **Broker-resting trailing stops (2c)** — attach `TRAILING_STOP` 5% on entry (R5).
- **Exit gate:** N clean sessions, P&L tracked, max-drawdown within limit, zero safety violations. Human review.

### Phase 3 — Anti-Martingale pyramiding (Gemini's core stock rule, on paper)
1. Stateless `strategies/pyramiding.py`: tranche ladder 100%→50%→25% at +10%/+20%, tranche level *derived* from broker position vs initial fill (SQLite projection), explicit unified 5% trailing stop as the exit.
2. **Stop consolidation protocol** in the router: place new consolidated stop only after the old stop's cancel reaches a *confirmed* terminal state (Gemini §7.1; `OrderState` machine already models this).
3. Risk-core addition: per-symbol tranche cap so the ladder can't exceed `max_position_qty`/exposure.
- **Exit gate:** full 3-tranche cycle executed and unwound cleanly on paper, audit trail complete.

### Phase 4 — Options income layer (Gemini's 50/200 matrix, on paper)
1. `strategies/covered_call.py`: 0.20-delta near-month strike finder composing vendored option-chain scripts; **lot-matching guard** ⌊shares/100⌋ − active short calls (covered-only, from broker truth).
2. 50/200 exits: BTC limit at −50% premium; stop at 3× entry premium; targets persisted per contract.
3. `RiskConfig.reserve_cash` buffer (Gemini §7.2) enforced by the risk core.
4. 08:30 job extension: ex-dividend lookup → stop adjustment (Gemini §7.3).
- **Exit gate:** one covered-call write + one 50% TP close on paper, never uncovered at any instant.

### Phase 5 — Narrow live (LIVE_SMALL) → Phase 6 — Broader live
Per research §6: two-key unlock (`TRADING_ENV=LIVE` + manual GUI), caps cut to ~10%, kill-switch tested live, fail-to-flat on OpenD loss, auto-demote to paper on any halt; then gradual cap raises, HK/crypto as separately-validated phases. **Promotion is human-only; demotion is automatic.**

---

## 5. Standing rules (unchanged, all phases)

Paper by default · no SDK unlock ever · no internet-reachable order path · broker is ground truth · no order without its audit line · UNKNOWN is never success · risk-limit changes require human review · every strategy stateless with explicit exits.
