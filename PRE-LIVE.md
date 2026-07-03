# PRE-LIVE Gate

**Rule: no live flag flip (`RISK_TRADING_ENV=LIVE`, `RISK_LIMIT_ORDERS_ENABLED=1`,
`RISK_REBALANCE_ENABLED=1`) while ANY box below is open.** Source: 2026-07-02
full-codebase review + spec `docs/superpowers/specs/2026-07-02-pre-live-hardening-design.md`.

## Blocking — code (spec workstreams)

- [x] W1 StopManager: morning stop re-attach + orphan sweep (review Critical #1;
      absorbs the option-leg orphan-rest blocker)
- [x] W2 Fail-loud broker queries (None ≠ []) + conservative call sites +
      snapshot cache (review Important #1)
- [x] W3 Trading calendar gates the whole loop (review Important #4)
- [x] W4 Durable engine state: deferred entries + at-most-once scheduler jobs
      (review Important #6, Minor #8)
- [x] W5 Escalation dwell + cancel-race guard + touch-priced MARKET risk check
      (review Important #3, T6b)
- [x] W6 Option CLOSE legs exempt from the entry premium budget (review Important #5)
- [x] W7 Runner is the sole performance writer (review Important #7)
- [x] W8 build_engine wires hedge_confirm_sleep/escalation_sleep = time.sleep
      (review Important #2)

## Blocking — operational

- [ ] The paper-only guards are consciously revised for the live cutover:
      `main()` refuses `trading_env != "PAPER"` (autotrader/main.py) and the
      risk core rejects non-PAPER envs (autotrader/risk_core.py). Both must be
      changed deliberately, with human review — never as a side effect.
- [ ] Trade password unlocked MANUALLY in the OpenD GUI (never via SDK).
- [ ] `RISK_MARKET_HOLIDAYS` extended past 2026 (shipped default covers 2026 only).
- [ ] Human live-session exit gate (pre-existing, from Phase 1/2).

## Non-blocking follow-ups (tracked, deferred by the 2026-07-02 spec §10)

- Early-close (half-day) calendar support — required before holiday-season live.
- SimBroker total_assets ignores position market value (Minor #3).
- Overlay legs record limit_price for MARKET orders (Minor #4).
- Gated-overlay signal-row spam (Minor #5).
- Escalation intermediate orders absent from the trades projection (Minor #6).
- Planner stock anchor is always MARKET — document or change (Minor #7).
- UNKNOWN-ack reconciliation job (Minor #9).
- Machine-local dates in db._today()/runner._compute_realized (Minor #1).
- db._conn reach-ins in EODReporter._gather / runner (Minor #2).
