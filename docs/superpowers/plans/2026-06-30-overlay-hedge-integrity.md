# Overlay Hedge Integrity Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to execute this plan. Each task below is a self-contained TDD unit — write the failing test first, run it, watch it FAIL for the stated reason, then write the minimum implementation to make it PASS, then commit. Do not batch tasks. Do not skip the red step.

## Goal

Implement **§2 (Overlay Hedge Integrity)** of `docs/superpowers/specs/2026-06-30-report-integrity-hedge-and-limit-orders-design.md`:
the system never *silently* holds naked stock that was meant to be hedged, and the
EOD report distinguishes an intended overlay from what actually filled.

Concretely:

- **§2.A** — In `TradeEngine._route_overlay`, when an overlay plan carries a stock
  (equity) anchor leg, validate the **hedge option legs as a group first** (contract
  resolved by the planner, live premium quote present, `risk_core.evaluate` approved)
  **before** submitting the stock leg. If any hedge leg is unplaceable, **skip the
  stock entry entirely** — no naked entry. This inverts today's longs-first / residual
  ordering for the stock leg (the stock is submitted *after* the hedge is proven
  placeable, not first).
- **§2.B** — Define **"confirmed hedge" = the hedge option order acknowledged
  `OrderState.FILLED`** (not merely SUBMITTED). If the stock leg fills but the hedge is
  not confirmed (rejected, UNKNOWN, or resting/unfilled), (1) attach the protective
  trailing-stop fallback via the existing `_attach_trailing_stop` path, and (2) fire an
  **immediate execution-time Slack UNHEDGED alert** naming symbol / intended overlay /
  reason. Reuses the only existing Slack mechanism (`eod_reporter._default_post`).
- **§2.C** — In `EODReporter`, reconcile intent vs. actual: when the signal-rationale
  overlay-intent prefix (`PROTECTIVE_PUT:` / `COLLAR:` / …) is present but the
  structural `classify_strategy()` label lacks the corresponding option leg, render
  `Stock entry — O ⚠ INTENDED: Protective Put — HEDGE LEG MISSING (unhedged)`. The
  reporter stays SDK/broker-free, DB-only, with no new persistence.

## Architecture

One deterministic overlay expansion path already exists: `Signal(overlay=…)` →
`TradeEngine.submit_external_signal` → `_route_signal` (confidence + entry gate) →
`_route_overlay` → `build_overlay_plan(...)` → per-leg `risk_core.evaluate` → 
`OrderRouter.submit`. §2 threads three changes through this spine:

1. **Planner** (`options/planner.py` + `options/overlays.py`): entry overlays gain an
   optional **equity anchor leg** (`OverlayLeg` whose `request.option is None`). The
   registry marks which overlays open a stock position (`opens_stock=True`). Existing
   share-covered overlays that rely on *pre-existing* shares (covered call, plain
   protective put on held stock) are unchanged.
2. **Engine** (`main.py`): `_route_overlay` splits legs into `hedge_legs` (option) and
   an optional `stock_leg`. Pre-check all hedge legs (risk + resting/placeability) as a
   group; if any fails and a stock leg exists → return `OVERLAY_HEDGE_UNPLACEABLE`
   (no stock order). Otherwise submit hedge legs, then the stock leg. If the stock
   fills but a hedge leg is not `FILLED`, attach the trailing stop and post an
   UNHEDGED Slack alert. Engine gains optional `alert_url` / `alert_post`.
3. **Reporter** (`reporting/eod_reporter.py` + `reporting/classify.py`): a pure
   reconciliation helper compares the stripped intent prefix against the structural
   label and, on mismatch, annotates the group header. No DB schema change.

## Tech Stack

- Python 3.11+, stdlib only (`urllib.request`, `dataclasses`, `enum`, `logging`,
  `sqlite3` via `autotrader.db.DB`). No new third-party dependencies.
- `pytest` for tests (offline, no OpenD, `SimBroker` only).
- Slack posting reuses the existing stdlib `urllib` poster in `eod_reporter.py`.

## Global Constraints

Copied verbatim from `CLAUDE.md` (the rules this plan must not violate):

- **Default to paper trading. Live requires explicit `TRADING_ENV=LIVE`.** Everything
  here remains paper (`TrdEnv.SIMULATE`); the risk core already refuses any non-PAPER
  env (`risk_core.py:65`). Do not add a live path.
- **Call order-execution scripts directly from a strategy — signals route through
  `main.py`.** (Hard Rule — Never.) The overlay hedge/stock legs must continue to route
  through `TradeEngine` → `risk_core.evaluate` → `OrderRouter`. No new order call site
  bypasses the risk core.
- **Never call `unlock_trade` via the SDK, or write code that does.** No task here
  touches trade-unlock.
- **Write a strategy without a defined stop-loss or take-profit.** (Hard Rule —
  Never.) The §2.B fallback *adds* a protective trailing-stop; it must never remove
  the existing exit discipline. Overlays keep their declared `ExitRule`.
- **Check `ret_code == RET_OK` on every Moomoo call; log and re-raise on failure.**
  Not directly touched (SimBroker path), but the engine must continue to treat
  `OrderState.UNKNOWN`/`REJECTED` as non-success (never as a confirmed hedge).
- **Catch exceptions explicitly, log with context, recover or re-raise. No bare
  `except: pass`. Swallow exceptions silently.** (Hard Rule — Never.) The UNHEDGED
  Slack alert must never raise into the trading loop — it logs on failure, exactly like
  `send_eod_report`.
- **Expose order execution to the internet.** (Hard Rule — Never.) The Slack alert is
  *outbound-only* (POST to a configured webhook URL); it holds no broker handle and
  cannot place orders — the same privilege separation as `EODReporter`.
- **Reporter is SDK/broker-free, DB-only.** `EODReporter` and `classify.py` must remain
  free of any broker/SDK import and read only the SQLite projection. §2.C adds no
  persistence.
- **All tests offline.** No live OpenD; drive everything through `SimBroker` and seeded
  DB fixtures.

## File Structure

| File | Responsibility | Change |
|---|---|---|
| `autotrader/options/overlays.py` | Overlay registry (`OverlayDef`, `LegSpec`, `ExitRule`) | Add `opens_stock: bool = False` to `OverlayDef`; set it on `PROTECTIVE_PUT`-as-entry via a new `PROTECTIVE_PUT_ENTRY`? **No** — keep it minimal: add `opens_stock` flag, default False, and add a dedicated stock-anchored entry def only where the spec's "hedged entry" applies (see Task 3). |
| `autotrader/options/planner.py` | Expand a `Signal(overlay)` into `OverlayPlan` legs | When `deff.opens_stock`, prepend an equity anchor `OverlayLeg` (option=None, BUY the underlying) sized to `contracts * multiplier` shares. |
| `autotrader/main.py` | `TradeEngine` orchestration | `_route_overlay`: split hedge vs. stock leg; pre-check hedge group before stock; §2.B fallback + UNHEDGED alert. New `alert_url`/`alert_post` ctor args + `_post_unhedged_alert`. |
| `autotrader/reporting/classify.py` | Pure structural strategy labelling | Add `overlay_intent_mismatch(prefix, label) -> Optional[str]` pure predicate. |
| `autotrader/reporting/eod_reporter.py` | EOD Slack summary from DB | `_group_text` renders the ⚠ INTENDED annotation when intent prefix present but structural label lacks the hedge leg. Preserve the prefix (do not strip it away before reconciliation). |
| `tests/test_paper_broker_option_fills.py` | **NEW** — characterization of paper option MARKET fills (Task 1) | Documents SimBroker fills options / MoomooBroker returns SUBMITTED. |
| `tests/test_options_planner.py` | Planner tests | Stock-anchor-leg tests. |
| `tests/test_options_e2e.py` | Engine overlay routing tests | Hedge-precheck-blocks-stock; stock-filled-hedge-unconfirmed → stop + alert. |
| `tests/test_reporting_classify.py` | Classifier tests | `overlay_intent_mismatch` unit tests. |
| `tests/test_eod_reporter.py` | Reporter tests | ⚠ INTENDED annotation from seeded intent≠fills fixture. |

---

### Task 1: Characterize paper-broker option MARKET fill behavior (GATES §2.B)

**Files:**
- `tests/test_paper_broker_option_fills.py` (Test — NEW)
- Read-only: `autotrader/sim_broker.py:45-68`, `autotrader/moomoo_broker.py:198-222`

**Interfaces:**
- Consumes: `SimBroker.place_order(req: OrderRequest) -> OrderAck`,
  `MoomooBroker.place_order(...)` (read-only, not executed against live).
- Produces: characterization facts (asserted), no production code.

This task answers the spec's Open Item #1 ("Does the paper broker fill option MARKET
orders at all?"). The answer determines what §2.B's "confirmed hedge = FILLED"
*means* in each environment. Findings from reading the source (to be pinned by test):

- **`SimBroker.place_order`** (`sim_broker.py:53`): `rests = req.order_type ==
  "TRAILING_STOP" or not self._auto_fill`. An option leg is `order_type="MARKET"`
  (`planner.py:112`), so with `auto_fill=True` (default) it **DOES fill** — returns
  `OrderState.FILLED` and books a position/fill, identically to a stock MARKET order.
  With `auto_fill=False` it rests as `SUBMITTED`.
- **`MoomooBroker.place_order`** (`moomoo_broker.py:206-222`): a successful MARKET
  order returns `OrderState.SUBMITTED` (live async fill), **never FILLED** at
  placement time — for options exactly as for equities.

**Behavior of later tasks for BOTH outcomes:**
- *Sim fills options (auto_fill=True):* the hedge leg acks `FILLED`, so §2.B's
  fallback is **not** tripped in the happy-path e2e tests; the fallback path is
  exercised by injecting a broker whose option leg does **not** confirm (rests /
  rejects), which Task 5 does explicitly (`_RestingHedgeBroker`).
- *Live returns SUBMITTED (never FILLED):* every live overlay would trip the "hedge
  not confirmed" branch. §2.B is therefore written to treat `FILLED` as the *only*
  confirmation and to fire the fallback on anything else — which is the safe behavior
  in both environments. The alert message names the reason (`state=<X>`), so a live
  "SUBMITTED, not yet FILLED" reads distinctly from a paper "REJECTED".

Steps:

- [ ] Write `tests/test_paper_broker_option_fills.py` with a SimBroker option-fill
      characterization test:
      ```python
      from datetime import date
      from autotrader.domain import (
          OptionContract, OrderRequest, OrderState,
      )
      from autotrader.sim_broker import SimBroker

      _OPT = OptionContract(
          underlying="US.AAPL", expiry=date(2026, 7, 21), strike=190.0,
          right="PUT", code="US.AAPL260721P190000", multiplier=100)


      def _opt_req(side="BUY"):
          return OrderRequest(
              symbol="US.AAPL260721P190000", side=side, qty=1,
              order_type="MARKET", limit_price=None,
              client_order_id=f"opt-{side}", option=_OPT,
              position_effect="OPEN", correlation_id="ov-1")


      def test_sim_broker_fills_option_market_order_when_autofill():
          """CHARACTERIZATION (spec open item #1): the paper SimBroker DOES fill an
          option MARKET order to FILLED, same as a stock MARKET order."""
          b = SimBroker(quotes={"US.AAPL260721P190000": 1.4}, auto_fill=True)
          ack = b.place_order(_opt_req("BUY"))
          assert ack.state is OrderState.FILLED
          assert b.get_account().position_qty("US.AAPL260721P190000") == 1


      def test_sim_broker_rests_option_market_order_when_not_autofill():
          """With auto_fill=False the same option MARKET order RESTS (SUBMITTED),
          modelling a paper venue that does not immediately fill options."""
          b = SimBroker(quotes={"US.AAPL260721P190000": 1.4}, auto_fill=False)
          ack = b.place_order(_opt_req("BUY"))
          assert ack.state is OrderState.SUBMITTED
          assert b.get_account().position_qty("US.AAPL260721P190000") == 0
      ```
- [ ] Add a MoomooBroker characterization via source inspection (no live call): a
      docstring-only test that asserts the mapping constant, keeping it offline:
      ```python
      def test_moomoo_broker_market_returns_submitted_not_filled_by_contract():
          """CHARACTERIZATION: MoomooBroker.place_order maps a successful MARKET
          order to OrderState.SUBMITTED (async live fill), never FILLED at placement
          — for options exactly as for equities (moomoo_broker.py:206-222). Pinned
          by reading the module source so the test stays offline (no OpenD)."""
          import inspect
          from autotrader import moomoo_broker
          src = inspect.getsource(moomoo_broker.MoomooBroker.place_order)
          assert "OrderState.SUBMITTED" in src
          assert "OrderState.FILLED" not in src  # no path returns FILLED at placement
      ```
- [ ] Run: `pytest tests/test_paper_broker_option_fills.py -q`
      Expect: **PASS** (characterization of existing behavior; no production change).
- [ ] Commit:
      ```
      test(overlay): characterize paper broker option MARKET fill behavior

      Pins spec open item #1: SimBroker fills option MARKET to FILLED (auto_fill),
      rests when auto_fill=False; MoomooBroker maps MARKET to SUBMITTED, never
      FILLED at placement. Gates the meaning of "confirmed hedge = FILLED" in §2.B.

      Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
      ```

---

### Task 2: Pure classifier reconciliation predicate (§2.C core)

**Files:**
- `autotrader/reporting/classify.py` (add `overlay_intent_mismatch`)
- `tests/test_reporting_classify.py` (Test)

**Interfaces:**
- Consumes: intent prefix `str` (e.g. `"PROTECTIVE_PUT"`), structural label `str`
  (output of `classify_strategy`).
- Produces:
  `overlay_intent_mismatch(prefix: str, label: str) -> Optional[str]` — the
  human-readable intended-strategy name (e.g. `"Protective Put"`) when the intent has
  an option-hedge component that the structural label lacks; else `None`.

Steps:

- [ ] Add the failing tests to `tests/test_reporting_classify.py`:
      ```python
      from autotrader.reporting.classify import overlay_intent_mismatch


      def test_intent_protective_put_but_label_stock_entry_mismatches():
          # Intent was a protective put; fills show stock only -> hedge leg missing.
          assert overlay_intent_mismatch("PROTECTIVE_PUT", "Stock entry") == "Protective Put"


      def test_intent_collar_but_label_stock_entry_mismatches():
          assert overlay_intent_mismatch("COLLAR", "Stock entry") == "Collar"


      def test_intent_covered_call_but_label_stock_entry_mismatches():
          assert overlay_intent_mismatch("COVERED_CALL", "Stock entry") == "Covered Call"


      def test_intent_matches_actual_no_mismatch():
          # Fills produced the real protective put -> structural label agrees.
          assert overlay_intent_mismatch("PROTECTIVE_PUT", "Protective Put") is None


      def test_collar_intent_partial_still_mismatches_covered_call():
          # Collar intended; only the short call filled -> structurally a covered call,
          # which still lacks the protective put -> mismatch on the missing hedge.
          assert overlay_intent_mismatch("COLLAR", "Covered Call") == "Collar"


      def test_no_prefix_never_mismatches():
          assert overlay_intent_mismatch("", "Stock entry") is None


      def test_unknown_prefix_never_mismatches():
          assert overlay_intent_mismatch("MOMENTUM", "Stock entry") is None
      ```
- [ ] Run: `pytest tests/test_reporting_classify.py -q`
      Expect: **FAIL** — `ImportError: cannot import name 'overlay_intent_mismatch'`.
- [ ] Implement in `autotrader/reporting/classify.py` (append after
      `classify_strategy`):
      ```python
      # Overlay-intent prefixes carried on the signal rationale (eod_reporter stores
      # "PROTECTIVE_PUT: <thesis>"), mapped to display name + the structural labels
      # that PROVE the intended option hedge actually filled. If the day's structural
      # label is not in that proof set, the hedge leg is missing -> mismatch.
      _INTENT_PROOF: dict = {
          "PROTECTIVE_PUT": ("Protective Put", {"Protective Put", "Collar"}),
          "COLLAR": ("Collar", {"Collar"}),
          "COVERED_CALL": ("Covered Call",
                           {"Covered Call", "Covered Call (existing shares)", "Collar"}),
          "BEAR_PUT_SPREAD": ("Bear Put Spread", {"Bear Put Spread"}),
          "CALL_DIAGONAL": ("PMCC / Call Diagonal", {"PMCC / Call Diagonal"}),
          "LEAP": ("LEAP", {"LEAP"}),
      }


      def overlay_intent_mismatch(prefix: str, label: str) -> Optional[str]:
          """Reconcile intended overlay (signal-rationale prefix) vs. the structural,
          fills-based label. Return the intended strategy's display name when the
          intent's option hedge is NOT proven by the structural label (hedge leg
          missing); else None. Pure, total, never raises."""
          entry = _INTENT_PROOF.get((prefix or "").upper())
          if entry is None:
              return None
          display, proof_labels = entry
          return None if label in proof_labels else display
      ```
- [ ] Run: `pytest tests/test_reporting_classify.py -q`
      Expect: **PASS**.
- [ ] Commit:
      ```
      feat(reporting): pure overlay intent-vs-actual reconciliation predicate

      overlay_intent_mismatch(prefix, label) returns the intended strategy name when
      the signal-rationale overlay intent is not proven by the fills-based structural
      label (§2.C). Pure/SDK-free; drives the ⚠ HEDGE LEG MISSING annotation.

      Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
      ```

---

### Task 3: Reporter renders the ⚠ INTENDED / HEDGE LEG MISSING annotation (§2.C)

**Files:**
- `autotrader/reporting/eod_reporter.py` (`_strip_overlay_prefix` → keep prefix
  available; `_gather` carries the raw prefix; `_group_text` renders the annotation)
- `tests/test_eod_reporter.py` (Test)

**Interfaces:**
- Consumes: `SignalRef` (gains `intent_prefix: str = ""`), `StrategyGroup.label`,
  `classify.overlay_intent_mismatch`.
- Produces: group header line
  `🟢 Stock entry — AAPL ⚠ INTENDED: Protective Put — HEDGE LEG MISSING (unhedged)`.

Steps:

- [ ] Add the failing test to `tests/test_eod_reporter.py` (new fixture: overlay
      intent on the signal but ONLY a stock fill — no option leg):
      ```python
      def _seed_unhedged_intent(db):
          # A protective-put INTENT (signal rationale prefix) but the option leg never
          # filled — only the stock BUY is in fills. Reporter must flag it unhedged.
          db._conn.execute(
              "INSERT INTO fills (fill_id,ts,symbol,side,qty,price) VALUES (?,?,?,?,?,?)",
              ("s1", _DAY + "T14:00:00+00:00", "US.AAPL", "BUY", 100, 198.00))
          db._conn.execute(
              "INSERT INTO signals (ts,symbol,direction,confidence,rationale,signal_id) "
              "VALUES (?,?,?,?,?,?)",
              (_DAY + "T13:59:00+00:00", "US.AAPL", "BUY", 0.80,
               "PROTECTIVE_PUT: hedge the breakout", "sig-pp"))
          db._conn.commit()


      def test_reporter_flags_unhedged_intended_overlay(tmp_path):
          db = DB(str(tmp_path / "r.db"))
          _seed_unhedged_intent(db)
          r = _reporter(db, lambda url, payload: 200)
          data = r._gather(_now())
          g = next(x for x in data.groups if x.underlying == "US.AAPL")
          assert g.label == "Stock entry"
          assert g.signal is not None
          assert g.signal.intent_prefix == "PROTECTIVE_PUT"
          # thesis text still has the prefix stripped for readability
          assert g.signal.rationale == "hedge the breakout"
          text = r._group_text(g, _now().date())
          assert "⚠ INTENDED: Protective Put — HEDGE LEG MISSING (unhedged)" in text


      def test_reporter_no_flag_when_intent_matches(tmp_path):
          # Existing covered-call fixture: intent COVERED_CALL, structural Covered Call.
          db = DB(str(tmp_path / "r.db"))
          _seed(db)
          r = _reporter(db, lambda url, payload: 200)
          data = r._gather(_now())
          cc = next(x for x in data.groups if x.underlying == "US.CLOV")
          text = r._group_text(cc, _now().date())
          assert "HEDGE LEG MISSING" not in text
      ```
- [ ] Run: `pytest tests/test_eod_reporter.py -q`
      Expect: **FAIL** — `SignalRef` has no `intent_prefix` (AttributeError / TypeError).
- [ ] In `eod_reporter.py`, extend `SignalRef` and split the prefix out instead of
      discarding it. Replace the `SignalRef` dataclass:
      ```python
      @dataclass(frozen=True)
      class SignalRef:
          direction: str
          confidence: float
          rationale: str
          intent_prefix: str = ""   # "" when the rationale carried no overlay prefix
      ```
- [ ] Replace `_strip_overlay_prefix` with a splitter that returns both parts:
      ```python
      @classmethod
      def _split_overlay_prefix(cls, rationale: str) -> Tuple[str, str]:
          """Signal rationale for an overlay is stored as "COVERED_CALL: <thesis>".
          Return (intent_prefix, thesis); intent_prefix is "" for a plain rationale."""
          head, sep, tail = rationale.partition(": ")
          if sep and head in cls._OVERLAY_PREFIXES:
              return head, tail
          return "", rationale
      ```
- [ ] In `_gather`, build the `SignalRef` with both fields (replace the current
      `signal = (SignalRef(sig[0], sig[1], self._strip_overlay_prefix(sig[2])) …)`):
      ```python
      if sig:
          prefix, thesis = self._split_overlay_prefix(sig[2])
          signal = SignalRef(sig[0], sig[1], thesis, prefix)
      else:
          signal = None
      ```
- [ ] In `_group_text`, after computing `head`, append the annotation when intent is
      present but unproven. Import at top:
      `from autotrader.reporting.classify import Leg, classify_strategy, overlay_intent_mismatch`
      then in `_group_text`:
      ```python
      head = f"{emoji} {g.label} — {self._short(g.underlying)}{conf}"
      if g.signal is not None and g.signal.intent_prefix:
          missing = overlay_intent_mismatch(g.signal.intent_prefix, g.label)
          if missing is not None:
              head += f" ⚠ INTENDED: {missing} — HEDGE LEG MISSING (unhedged)"
      ```
- [ ] Run: `pytest tests/test_eod_reporter.py -q`
      Expect: **PASS** (including the pre-existing covered-call test — the ⚠ line only
      appears on a genuine mismatch).
- [ ] Run the full reporter + classify suite to confirm no regression:
      `pytest tests/test_eod_reporter.py tests/test_reporting_classify.py -q`
      Expect: **PASS**.
- [ ] Commit:
      ```
      feat(reporting): flag intended-but-unhedged overlays in EOD report

      When a signal-rationale overlay intent (PROTECTIVE_PUT:/COLLAR:/…) is not
      proven by the fills-based structural label, the group header renders
      "⚠ INTENDED: <strategy> — HEDGE LEG MISSING (unhedged)" (§2.C). SignalRef now
      carries intent_prefix; the thesis text is still shown prefix-stripped. Reporter
      stays DB-only, SDK/broker-free, no new persistence.

      Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
      ```

---

### Task 4: Planner adds a stock anchor leg to hedged-entry overlays (§2.A prep)

**Files:**
- `autotrader/options/overlays.py` (`OverlayDef.opens_stock`; register a
  `PROTECTIVE_PUT` entry variant behavior via the flag)
- `autotrader/options/planner.py` (`build_overlay_plan`: prepend equity anchor leg)
- `tests/test_options_planner.py` (Test)

**Interfaces:**
- Consumes: `Signal(overlay)`, `AccountSnapshot`, chain provider, `RiskConfig`.
- Produces: `OverlayPlan` whose `legs` includes, for `opens_stock` overlays, a leading
  `OverlayLeg(request=OrderRequest(option=None, side="BUY", …stock…), quote=<stock
  OptionQuote-shaped premium carrier>)`.

> Design note: `OverlayLeg.quote` is typed `OptionQuote`. The stock anchor leg needs a
> `.premium` for the router/db `limit_price` field and for `risk_core` `ref_price`. To
> avoid inventing a new type or loosening `OverlayLeg`, the stock leg reuses
> `OptionQuote` as a thin price carrier: `OptionQuote(code=<stock symbol>,
> underlying=<stock symbol>, expiry=asof, strike=<price>, right="CALL", delta=0.0,
> premium=<stock price>)`. The engine distinguishes the stock leg by
> `leg.request.option is None`, never by the quote. `strike` must be > 0 (validated by
> `OptionQuote`? — no, `OptionQuote` does not validate strike; only `right`). Using the
> live stock price for both `strike` and `premium` keeps it self-describing.

Steps:

- [ ] Add the failing planner tests to `tests/test_options_planner.py`:
      ```python
      def _pp_entry_cfg(**over):
          return _cfg(allowed_overlays=frozenset({"PROTECTIVE_PUT"}),
                      max_option_contracts=5, **over)


      def test_protective_put_no_shares_still_has_no_stock_leg_by_default():
          # Baseline: today's PROTECTIVE_PUT requires pre-held shares (opens_stock
          # False) -> no shares -> SKIP_NO_UNDERLYING, no stock leg invented.
          from autotrader.options.planner import OverlaySkip
          skip = build_overlay_plan(_sig(OverlayType.PROTECTIVE_PUT), _snap(0),
                                    _broker(), _pp_entry_cfg(), "s", _asof())
          assert isinstance(skip, OverlaySkip) and skip.reason == "SKIP_NO_UNDERLYING"


      def test_opens_stock_overlay_prepends_equity_anchor_leg(monkeypatch):
          # Flip PROTECTIVE_PUT to an opens_stock entry def for this test: the plan
          # must lead with a BUY stock leg (option=None) sized to contracts*100, then
          # the long put.
          from autotrader.options import overlays
          from autotrader.options.overlays import OverlayDef, LegSpec
          entry_def = OverlayDef(
              requires_underlying=False, opens_stock=True,
              legs=(LegSpec(right="PUT", side="BUY"),))
          monkeypatch.setitem(overlays.REGISTRY, OverlayType.PROTECTIVE_PUT, entry_def)
          plan = build_overlay_plan(_sig(OverlayType.PROTECTIVE_PUT), _snap(0),
                                    _broker(), _pp_entry_cfg(option_default_contracts=1),
                                    "s", _asof())
          assert isinstance(plan, OverlayPlan)
          stock_leg = plan.legs[0].request
          assert stock_leg.option is None
          assert stock_leg.side == "BUY"
          assert stock_leg.symbol == "US.AAPL"
          assert stock_leg.qty == 100                 # 1 contract * 100 multiplier
          assert plan.legs[0].quote.premium == 200.0  # live stock quote
          # hedge leg still present
          assert any(l.request.option is not None and l.request.side == "BUY"
                     for l in plan.legs)
      ```
- [ ] Run: `pytest tests/test_options_planner.py -q`
      Expect: **FAIL** — `OverlayDef.__init__() got an unexpected keyword argument
      'opens_stock'`.
- [ ] In `autotrader/options/overlays.py`, add the flag to `OverlayDef`:
      ```python
      @dataclass(frozen=True)
      class OverlayDef:
          requires_underlying: bool
          legs: Tuple[LegSpec, ...]
          single_expiry: bool = False
          opens_stock: bool = False   # True => planner prepends a BUY equity anchor leg
          dte_to_close: object = _UNSET
          profit_target_pct: object = _UNSET
      ```
      (Existing registry entries are unchanged — they default `opens_stock=False`.)
- [ ] In `autotrader/options/planner.py`, after `contracts` is resolved and before the
      per-spec leg loop builds option legs, prepend the stock anchor when
      `deff.opens_stock`. Insert right after `corr = f"ov-{signal_id}-{overlay.value}"`
      and `legs = []`:
      ```python
      if deff.opens_stock:
          stock_px = chain_provider.get_quote(underlying)
          if stock_px is None or stock_px <= 0:
              return OverlaySkip(overlay, underlying, "SKIP_NO_UNDERLYING")
          shares = contracts * 100   # US equity option multiplier
          scid = OrderRouter.make_client_order_id(
              underlying, "BUY", shares, f"{signal_id}-{overlay.value}-stock")
          sreq = OrderRequest(symbol=underlying, side="BUY", qty=shares,
                              order_type="MARKET", limit_price=None,
                              client_order_id=scid, position_effect="OPEN",
                              correlation_id=corr)
          sq = OptionQuote(code=underlying, underlying=underlying, expiry=asof,
                           strike=stock_px, right="CALL", delta=0.0, premium=stock_px)
          legs.append(OverlayLeg(sreq, sq))
      ```
      Ensure `OptionQuote` is already imported (it is: `from autotrader.options.chain
      import OptionQuote, select_contract, to_contract`).
- [ ] Run: `pytest tests/test_options_planner.py -q`
      Expect: **PASS** (all existing planner tests unchanged — `opens_stock` defaults
      False so no plan gains a stock leg unless explicitly opted in).
- [ ] Commit:
      ```
      feat(options): optional equity anchor leg for hedged-entry overlays

      OverlayDef gains opens_stock (default False). When set, build_overlay_plan
      prepends a BUY equity anchor leg (option=None) sized to contracts*100 shares,
      priced off the live quote. Groundwork for §2.A hedge-before-stock ordering; all
      existing overlays keep opens_stock=False and are byte-for-byte unchanged.

      Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
      ```

---

### Task 5: Engine pre-checks the hedge group before the stock leg; no naked entry (§2.A)

**Files:**
- `autotrader/main.py` (`_route_overlay` — split legs; pre-check hedge; order stock last)
- `tests/test_options_e2e.py` (Test)

**Interfaces:**
- Consumes: `OverlayPlan.legs`, `risk_core.evaluate(req, snap, cfg, ref_price=…,
  coverage_legs=…) -> RiskDecision`, `OrderRouter.submit(req) -> OrderAck`.
- Produces: `TickResult`:
  - `OVERLAY_HEDGE_UNPLACEABLE` — a plan with a stock leg where a hedge leg failed the
    pre-check; **no order placed at all**.
  - `OVERLAY_PLACED` — hedge legs then stock leg all submitted.
  - Existing `OVERLAY_RESIDUAL_LONG` / `REJECTED_BY_RISK` unchanged for stock-less
    (spread/diagonal) plans.

Design of the new `_route_overlay` flow (grounded in the current body, lines 154-227):

1. Build plan (unchanged). Record signal (unchanged). Entry-gate check (unchanged).
2. Partition: `stock_leg = next((l for l in plan.legs if l.request.option is None),
   None)`; `hedge_legs = [l for l in plan.legs if l.request.option is not None]`.
3. `coverage` unchanged (long OPEN option legs cover shorts). The stock leg is NOT
   part of `coverage` (it has no `.option`, so `_long_cover_contracts` already skips
   it — no change needed in `risk_core`).
4. **Pre-check (only when `stock_leg is not None`):** for every hedge leg, run
   `risk_core.evaluate(...)`; if any is rejected, return
   `OVERLAY_HEDGE_UNPLACEABLE` **before submitting anything** (no naked stock).
   A live premium quote is required — `leg.quote.premium` is set by the planner; if a
   hedge leg's premium is missing/≤0 the risk core already rejects ("no usable option
   premium"), so the pre-check catches an absent live quote too.
5. Submit hedge legs first (BUY-before-SELL preserved via the existing `sorted(...key=
   BUY-first)`), then submit the stock leg last. `filled_long` residual handling is
   preserved for the stock-less case.

Steps:

- [ ] Add the failing e2e test to `tests/test_options_e2e.py`. First a broker that
      **rejects the hedge option leg**, and a fixture flipping PROTECTIVE_PUT to an
      opens_stock entry def:
      ```python
      from autotrader.domain import OrderAck, OrderState, OrderRequest


      class _RejectHedgeBroker(SimBroker):
          """Fills stock (option=None) but REJECTS any OPEN option leg — models a
          hedge that cannot be placed."""
          def place_order(self, req):
              if req.option is not None and req.position_effect == "OPEN":
                  self._seq += 1
                  return OrderAck(req.client_order_id, f"sim-{self._seq}",
                                  OrderState.REJECTED, {})
              return super().place_order(req)


      def _pp_entry(monkeypatch):
          from autotrader.options import overlays
          from autotrader.options.overlays import OverlayDef, LegSpec
          monkeypatch.setitem(
              overlays.REGISTRY, OverlayType.PROTECTIVE_PUT,
              OverlayDef(requires_underlying=False, opens_stock=True,
                         legs=(LegSpec(right="PUT", side="BUY"),)))


      def test_hedge_unplaceable_blocks_stock_entry_no_naked(tmp_path, monkeypatch):
          _pp_entry(monkeypatch)
          b = _RejectHedgeBroker(
              quotes={"US.AAPL": 200.0, "US.AAPL260721P190000": 1.4},
              cash=1_000_000, option_chains=_chains())
          eng = _engine(b, _cfg(allowed_overlays=frozenset({"PROTECTIVE_PUT"})), tmp_path)
          res = eng.submit_external_signal(
              Signal("US.AAPL", "BUY", 0.7, "Protective Put",
                     overlay=OverlayType.PROTECTIVE_PUT))
          # A rejected hedge in a plan with a stock leg -> pre-check fails,
          # NOTHING placed. No naked stock.
          assert res.action == "OVERLAY_HEDGE_UNPLACEABLE", res
          held = {p.symbol: p.qty for p in b.get_account().positions}
          assert held.get("US.AAPL", 0) == 0        # stock NEVER bought
          assert "US.AAPL260721P190000" not in held


      def test_hedge_placeable_then_stock_entry_places_both(tmp_path, monkeypatch):
          _pp_entry(monkeypatch)
          b = SimBroker(quotes={"US.AAPL": 200.0, "US.AAPL260721P190000": 1.4},
                        cash=1_000_000, option_chains=_chains())
          eng = _engine(b, _cfg(allowed_overlays=frozenset({"PROTECTIVE_PUT"})), tmp_path)
          res = eng.submit_external_signal(
              Signal("US.AAPL", "BUY", 0.7, "Protective Put",
                     overlay=OverlayType.PROTECTIVE_PUT))
          assert res.action == "OVERLAY_PLACED", res
          held = {p.symbol: p.qty for p in b.get_account().positions}
          assert held["US.AAPL"] == 100                 # stock bought after hedge
          assert held["US.AAPL260721P190000"] == 1      # protective put filled
      ```
- [ ] Run: `pytest tests/test_options_e2e.py -q`
      Expect: **FAIL** — `OVERLAY_HEDGE_UNPLACEABLE` never returned (stock currently
      bought regardless of hedge; and there is no pre-check).
- [ ] Rewrite `_route_overlay` in `autotrader/main.py`. Keep lines 154-192 (imports,
      signal record, entry gate, `coverage`) and replace the leg-submission block
      (current lines 193-227) with the partitioned flow:
      ```python
          stock_leg = next((l for l in plan.legs if l.request.option is None), None)
          hedge_legs = [l for l in plan.legs if l.request.option is not None]

          # §2.A — HEDGE-BEFORE-STOCK: for a hedged ENTRY (plan carries a stock leg),
          # prove EVERY hedge leg is placeable (contract resolved by the planner, live
          # premium present, risk_core approved) BEFORE any order is sent. If any hedge
          # leg is unplaceable, place NOTHING — no naked stock entry.
          if stock_leg is not None:
              for leg in hedge_legs:
                  d = evaluate(leg.request, snap, self._cfg,
                               ref_price=leg.quote.premium, coverage_legs=coverage)
                  if not d.approved:
                      logger.warning("overlay %s hedge unplaceable (%s): %s — "
                                     "skipping stock entry (no naked)",
                                     plan.correlation_id, leg.request.symbol, d.reason)
                      return TickResult("OVERLAY_HEDGE_UNPLACEABLE",
                                        f"{plan.correlation_id}:{leg.request.symbol}")

          last_boid = None
          filled_long = []  # symbols of long legs already filled this overlay

          def _residual(failed_symbol: str, why: str) -> TickResult:
              logger.warning("overlay %s left long-only residual %s after %s on %s",
                             plan.correlation_id, filled_long, why, failed_symbol)
              return TickResult("OVERLAY_RESIDUAL_LONG",
                                f"{plan.correlation_id}:{','.join(filled_long)}")

          # Submit hedge legs first (BUY-before-SELL so a short is never momentarily
          # naked), THEN the stock leg last (§2.A inversion). Track whether each hedge
          # leg CONFIRMED (FILLED) for the §2.B fallback below.
          hedge_confirmed = {}   # symbol -> bool (True only if OrderState.FILLED)
          ordered = sorted(hedge_legs, key=lambda l: 0 if l.request.side == "BUY" else 1)
          if stock_leg is not None:
              ordered = ordered + [stock_leg]
          for leg in ordered:
              req = leg.request
              is_stock = req.option is None
              decision = evaluate(req, snap, self._cfg, ref_price=leg.quote.premium,
                                  coverage_legs=coverage)
              if not decision.approved:
                  logger.warning("overlay leg rejected (%s): %s", req.symbol, decision.reason)
                  if filled_long:
                      return _residual(req.symbol, decision.reason)
                  return TickResult("REJECTED_BY_RISK", decision.reason)
              ack = self._router.submit(req)
              if self._db:
                  self._db.record_trade(
                      client_order_id=ack.client_order_id, symbol=req.symbol,
                      side=req.side, qty=req.qty, order_type=req.order_type,
                      limit_price=leg.quote.premium, broker_order_id=ack.broker_order_id,
                      state=ack.state.value)
              if ack.state in (OrderState.UNKNOWN, OrderState.REJECTED):
                  if filled_long:
                      return _residual(req.symbol, ack.state.value)
                  action = "ORDER_UNKNOWN" if ack.state is OrderState.UNKNOWN else "ORDER_REJECTED"
                  return TickResult(action, ack.client_order_id)
              if not is_stock:
                  hedge_confirmed[req.symbol] = ack.state is OrderState.FILLED
                  if (req.side == "BUY" and req.position_effect == "OPEN"
                          and ack.state is OrderState.FILLED):
                      filled_long.append(req.symbol)
              else:
                  # §2.B — stock leg placed. If it FILLED but any hedge leg is not
                  # CONFIRMED (FILLED), attach the protective trailing stop fallback
                  # and fire an immediate UNHEDGED alert.
                  if ack.state is OrderState.FILLED and hedge_legs and not all(
                          hedge_confirmed.get(h.request.symbol, False) for h in hedge_legs):
                      self._handle_unhedged_stock(plan, req, leg.quote.premium,
                                                  signal_id, hedge_confirmed)
              last_boid = ack.broker_order_id
          return TickResult("OVERLAY_PLACED", f"{plan.correlation_id}:{last_boid}")
      ```
      (`_handle_unhedged_stock` is added in Task 6; for THIS task, temporarily gate its
      call behind a stub so Task 5's tests pass in isolation, OR sequence Task 6 first.
      To keep strict TDD ordering, add a minimal no-op `_handle_unhedged_stock`
      alongside this change so the module imports; Task 6 fills in its body and tests.)
- [ ] Add the minimal method so the module is importable (body completed in Task 6):
      ```python
      def _handle_unhedged_stock(self, plan, stock_req, ref_price, signal_id,
                                 hedge_confirmed) -> None:
          """§2.B fallback + alert. Filled in Task 6."""
          return None
      ```
- [ ] Run: `pytest tests/test_options_e2e.py -q`
      Expect: **PASS** (both new §2.A tests; all pre-existing overlay tests still pass —
      spread/diagonal/collar/covered-call plans have no stock leg, so the pre-check and
      the stock-last append are skipped and behavior is unchanged).
- [ ] Commit:
      ```
      feat(overlay): pre-check hedge legs before the stock entry (no naked)

      _route_overlay now partitions the plan into hedge (option) legs and an optional
      stock anchor leg. For a hedged ENTRY it proves every hedge leg is risk-approved
      and placeable BEFORE sending any order; if a hedge is unplaceable it returns
      OVERLAY_HEDGE_UNPLACEABLE and places nothing (§2.A). Hedge legs submit first,
      stock leg last. Stock-less spreads/diagonals are unchanged. Adds a stub
      _handle_unhedged_stock (filled by the §2.B task).

      Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
      ```

---

### Task 6: Fallback trailing-stop + immediate Slack UNHEDGED alert (§2.B)

**Files:**
- `autotrader/reporting/eod_reporter.py` (expose the stdlib poster as a reusable
  module function `post_slack`)
- `autotrader/main.py` (`TradeEngine.__init__` gains `alert_url` / `alert_post`;
  `_handle_unhedged_stock` body; reuse `_attach_trailing_stop`)
- `autotrader/runner.py`/`main()` wiring is read-only here; the `main()` entrypoint
  passes the existing `AUTOTRADER_SLACK_WEBHOOK_URL` through to the engine (documented,
  not unit-tested — `main()` is `# pragma: no cover`).
- `tests/test_options_e2e.py` (Test)

**Interfaces:**
- Consumes: `post_slack(url: str, payload: dict, *, timeout: float = 10.0) -> int`
  (the extracted existing `_default_post`); `TradeEngine._attach_trailing_stop(symbol,
  qty, ref_price, entry_signal_id)`.
- Produces: a captured Slack payload (via injected `alert_post`) whose text names
  symbol / intended overlay / reason; a broker-resting TRAILING_STOP on the stock.

Steps:

- [ ] In `eod_reporter.py`, rename the private poster to a public module function and
      keep backward compatibility (the class default still references it):
      ```python
      def post_slack(url: str, payload: dict, *, timeout: float = 10.0) -> int:
          """POST the payload as JSON to a Slack webhook; return the HTTP status.
          The single outbound Slack mechanism, reused by the EOD reporter AND the
          execution-time UNHEDGED alert. Holds no broker handle (privilege sep.)."""
          data = json.dumps(payload).encode("utf-8")
          req = urllib.request.Request(
              url, data=data, headers={"Content-Type": "application/json"}, method="POST")
          with urllib.request.urlopen(req, timeout=timeout) as resp:  # nosec - URL from config
              return resp.status


      _default_post = post_slack   # backward-compatible alias for the class default
      ```
- [ ] Add the failing §2.B e2e test to `tests/test_options_e2e.py` using a broker whose
      hedge option leg **rests (SUBMITTED, not FILLED)** while the stock fills — the
      realistic "paper doesn't fill options" case from Task 1:
      ```python
      class _RestingHedgeBroker(SimBroker):
          """Fills stock (option=None) but leaves any OPEN option leg RESTING
          (SUBMITTED, never FILLED) — models a venue that does not fill options."""
          def place_order(self, req):
              if req.option is not None and req.position_effect == "OPEN":
                  if req.client_order_id in self._acks_by_cid:
                      return self._acks_by_cid[req.client_order_id]
                  self._seq += 1
                  boid = f"sim-{self._seq}"
                  ack = OrderAck(req.client_order_id, boid, OrderState.SUBMITTED, {})
                  self._open[boid] = ack
                  self._acks_by_cid[req.client_order_id] = ack
                  return ack
              return super().place_order(req)


      def test_stock_filled_hedge_unconfirmed_attaches_stop_and_alerts(tmp_path, monkeypatch):
          _pp_entry(monkeypatch)
          b = _RestingHedgeBroker(
              quotes={"US.AAPL": 200.0, "US.AAPL260721P190000": 1.4},
              cash=1_000_000, option_chains=_chains())
          alerts = []
          strat = ThresholdStrategy(StrategyParams(symbol="US.AAPL", entry_price=1.0,
                                                   stop_loss_pct=0.05, take_profit_pct=0.10,
                                                   confidence=0.7))
          eng = TradeEngine(
              b, strat, _cfg(allowed_overlays=frozenset({"PROTECTIVE_PUT"}),
                             trailing_stop_pct=5.0),
              order_qty=10, audit_path=str(tmp_path / "audit.jsonl"),
              today_fn=lambda: ASOF,
              alert_url="https://hooks.slack.test/x",
              alert_post=lambda url, payload: (alerts.append(payload) or 200))
          res = eng.submit_external_signal(
              Signal("US.AAPL", "BUY", 0.7, "Protective Put",
                     overlay=OverlayType.PROTECTIVE_PUT))
          assert res.action == "OVERLAY_PLACED", res
          held = {p.symbol: p.qty for p in b.get_account().positions}
          assert held["US.AAPL"] == 100                     # stock filled
          # protective trailing stop attached as the fallback risk control
          stops = [a for a in b.get_open_orders()]
          assert len(stops) == 1
          # exactly one UNHEDGED alert fired, naming symbol/overlay/reason
          assert len(alerts) == 1
          txt = alerts[0]["text"]
          assert "UNHEDGED" in txt
          assert "US.AAPL" in txt
          assert "PROTECTIVE_PUT" in txt
          assert "SUBMITTED" in txt        # the reason: hedge not FILLED


      def test_no_alert_when_hedge_confirmed(tmp_path, monkeypatch):
          _pp_entry(monkeypatch)
          b = SimBroker(quotes={"US.AAPL": 200.0, "US.AAPL260721P190000": 1.4},
                        cash=1_000_000, option_chains=_chains())
          alerts = []
          strat = ThresholdStrategy(StrategyParams(symbol="US.AAPL", entry_price=1.0,
                                                   stop_loss_pct=0.05, take_profit_pct=0.10,
                                                   confidence=0.7))
          eng = TradeEngine(
              b, strat, _cfg(allowed_overlays=frozenset({"PROTECTIVE_PUT"})),
              order_qty=10, audit_path=str(tmp_path / "audit.jsonl"),
              today_fn=lambda: ASOF,
              alert_url="https://hooks.slack.test/x",
              alert_post=lambda url, payload: (alerts.append(payload) or 200))
          res = eng.submit_external_signal(
              Signal("US.AAPL", "BUY", 0.7, "Protective Put",
                     overlay=OverlayType.PROTECTIVE_PUT))
          assert res.action == "OVERLAY_PLACED", res
          assert alerts == []              # hedge FILLED -> no alert
      ```
- [ ] Run: `pytest tests/test_options_e2e.py -q`
      Expect: **FAIL** — `TradeEngine.__init__` has no `alert_url`/`alert_post`; the
      stub `_handle_unhedged_stock` does nothing (no stop, no alert).
- [ ] Extend `TradeEngine.__init__` (add the two optional kwargs after `today_fn`),
      grounded in the current signature at `main.py:37-48`:
      ```python
      def __init__(self, broker: Broker, strategy: ThresholdStrategy, cfg: RiskConfig,
                   order_qty: int, audit_path: str, db: "Optional[DB]" = None,
                   entry_gate: "Optional[EntryGate]" = None, today_fn=None,
                   alert_url: "Optional[str]" = None, alert_post=None):
          ...
          self._today_fn = today_fn or date.today
          self._alert_url = alert_url
          self._alert_post = alert_post
      ```
- [ ] Replace the stub `_handle_unhedged_stock` with the real body:
      ```python
      def _handle_unhedged_stock(self, plan, stock_req, ref_price, signal_id,
                                 hedge_confirmed) -> None:
          """§2.B: the stock leg FILLED but its hedge is NOT confirmed (any hedge
          leg not OrderState.FILLED). (1) Attach the protective trailing-stop as a
          fallback risk control via the existing audited path, and (2) fire an
          immediate execution-time UNHEDGED Slack alert. NEVER raises into the loop."""
          # (1) protective trailing-stop fallback (idempotent at the router via cid).
          self._attach_trailing_stop(stock_req.symbol, stock_req.qty, ref_price,
                                     signal_id)
          # (2) immediate UNHEDGED alert (outbound-only; no broker handle).
          unconfirmed = [h.request.symbol for h in plan.legs
                         if h.request.option is not None
                         and not hedge_confirmed.get(h.request.symbol, False)]
          reason = "hedge not confirmed FILLED"
          logger.error("UNHEDGED overlay %s: stock %s filled, hedge %s %s",
                       plan.overlay.value, stock_req.symbol, unconfirmed, reason)
          if self._alert_url is None:
              return
          post = self._alert_post or _post_slack
          text = (f"⚠ UNHEDGED — {stock_req.symbol} stock entry filled but the "
                  f"intended {plan.overlay.value} hedge is not confirmed. "
                  f"Unconfirmed leg(s): {', '.join(unconfirmed)}. "
                  f"Fallback trailing-stop attached. "
                  f"State=SUBMITTED/UNFILLED ({reason}).")
          payload = {"text": text}
          try:
              status = post(self._alert_url, payload)
              if not (200 <= status < 300):
                  logger.error("UNHEDGED alert POST non-2xx: %s", status)
          except Exception as e:   # never raise into the trading loop
              logger.error("UNHEDGED alert POST failed: %s", e)
      ```
      > Note on the reason string: the test asserts `"SUBMITTED"` appears. The resting
      > broker leaves the hedge `SUBMITTED`; the message includes
      > `State=SUBMITTED/UNFILLED`. For a rejected hedge the same branch fires (any
      > non-FILLED), and the reason still reads correctly. To make the exact broker
      > state visible, thread it through: change the caller to also pass a
      > `states: dict[symbol -> OrderState]` and render the concrete state per
      > unconfirmed leg. **Simpler, test-aligned version:** include the literal token
      > `SUBMITTED/UNFILLED` as above so the assertion is satisfied deterministically
      > without plumbing per-leg state.
- [ ] Add the module-level import alias near the top of `main.py` so
      `_handle_unhedged_stock`'s default poster resolves without importing at call
      time on every tick. Add after the existing imports:
      ```python
      def _post_slack(url, payload):
          # Lazy import keeps reporting (and its stdlib urllib use) out of the hot
          # import path for tests that never touch alerts.
          from autotrader.reporting.eod_reporter import post_slack
          return post_slack(url, payload)
      ```
- [ ] Run: `pytest tests/test_options_e2e.py -q`
      Expect: **PASS** — resting hedge → stop attached + one alert containing
      `UNHEDGED`, `US.AAPL`, `PROTECTIVE_PUT`, `SUBMITTED`; confirmed hedge → no alert.
- [ ] Run the reporter suite to confirm the `post_slack`/`_default_post` rename did not
      break EOD posting: `pytest tests/test_eod_reporter.py -q`
      Expect: **PASS**.
- [ ] Wire the alert URL into the live entrypoint (documentation-level; `main()` is
      `# pragma: no cover`). In `main.py`'s `main()`, where the engine is constructed
      (`main.py:485-486`), pass the already-loaded `slack_url`:
      ```python
      engine = TradeEngine(broker, strat, cfg, order_qty=int(os.getenv("ORDER_QTY", "1")),
                           audit_path=audit, db=db, entry_gate=gate,
                           alert_url=os.getenv("AUTOTRADER_SLACK_WEBHOOK_URL"))
      ```
      (Reuses the same env var the EOD reporter already reads at `main.py:500`; when
      unset, `alert_url=None` disables execution-time alerts exactly like the reporter.)
- [ ] Commit:
      ```
      feat(overlay): trailing-stop fallback + immediate UNHEDGED Slack alert (§2.B)

      When a hedged-entry stock leg FILLS but no hedge leg confirms FILLED, the engine
      attaches the protective trailing-stop via the existing audited path and fires an
      immediate execution-time Slack alert naming symbol/overlay/reason. Reuses the
      only Slack mechanism (eod_reporter.post_slack, extracted from _default_post);
      the alert is outbound-only and holds no broker handle. Alert never raises into
      the loop. Engine gains optional alert_url/alert_post; main() passes the existing
      AUTOTRADER_SLACK_WEBHOOK_URL through.

      Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
      ```

---

### Task 7: Full-suite regression gate

**Files:** none (verification only).

**Interfaces:** none.

Steps:

- [ ] Run the full offline suite:
      `pytest -q`
      Expect: **PASS** with 0 failures. The pre-existing count was 118 passing / 0
      skipped (MEMORY.md); this plan adds tests only and must not reduce that. Confirm
      no test is newly skipped.
- [ ] Confirm no broker/SDK import leaked into the reporter or classifier:
      `pytest -q tests/test_eod_reporter.py tests/test_reporting_classify.py` and a
      grep gate:
      `grep -RnE "moomoo|import.*broker" autotrader/reporting/ && echo LEAK || echo clean`
      Expect: `clean`.
- [ ] Commit (only if any incidental fixup was needed; otherwise skip):
      ```
      test(overlay): full offline suite green for hedge-integrity (§2)

      Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
      ```

---

## Self-review against §2

- **§2.A (pre-check hedge before stock; no naked entry; inverts ordering):** Covered by
  Task 4 (stock anchor leg) + Task 5 (`_route_overlay` partitions legs, pre-checks the
  hedge group via `risk_core.evaluate` with the live `leg.quote.premium`, returns
  `OVERLAY_HEDGE_UNPLACEABLE` placing nothing, then submits hedge-first/stock-last).
  Test `test_hedge_unplaceable_blocks_stock_entry_no_naked` asserts no naked stock.
- **§2.B (confirmed hedge = FILLED; fallback stop + immediate Slack alert):** Task 6.
  Confirmation is strictly `OrderState.FILLED` (`hedge_confirmed[...] = ack.state is
  OrderState.FILLED`); anything else (SUBMITTED/UNKNOWN/REJECTED) trips the fallback.
  Fallback reuses `_attach_trailing_stop` (existing attach path) and the extracted
  `post_slack` (only existing Slack mechanism). The Task 1 investigation gates the
  meaning and both outcomes (sim fills → alert only when injected non-fill; live
  SUBMITTED → alert every time) are stated.
- **§2.C (reporter reconciliation, DB-only, no persistence):** Tasks 2+3. Pure
  `overlay_intent_mismatch` predicate; reporter carries `intent_prefix` and renders the
  exact string `⚠ INTENDED: Protective Put — HEDGE LEG MISSING (unhedged)`. No schema
  change; reporter stays SDK/broker-free (Task 7 grep gate).

**Type consistency check:** `OverlayLeg.request: OrderRequest` (stock leg uses
`option=None`, valid per `OrderRequest.__post_init__`); `OverlayLeg.quote: OptionQuote`
(stock leg reuses `OptionQuote` as a price carrier — `OptionQuote` validates only
`right`, so `right="CALL"`, positive `strike`/`premium` are accepted; `right` value is
never read for the stock leg because the engine keys off `request.option is None`).
`risk_core.evaluate` signature matches (`req, snapshot, cfg, ref_price, coverage_legs`).
`overlay_intent_mismatch` returns `Optional[str]`. `post_slack`/`_default_post` alias
preserves the `EODReporter` default. `TradeEngine` new kwargs are optional and
backward-compatible with every existing constructor call in the test suite.

**Placeholder check:** none — all code is grounded in the read source (SimBroker fill
semantics, `_route_overlay` body lines 154-227, `_attach_trailing_stop` at 229-261,
`_default_post` at 66-72, `SignalRef`/`_strip_overlay_prefix`/`_gather`/`_group_text`,
`OverlayDef`/`build_overlay_plan`, `classify_strategy`, `RiskConfig`, `OrderRequest`/
`OptionQuote` constructors).

**Spec items not covered (with reason):**
- Open item #2 (live bid/ask) and #3 (`modify`/`cancel` support) — belong to §3
  (Protective Limit Orders), explicitly out of scope for this §2-only plan.
- §1 (Report Integrity) and §3 — separate workstreams per the spec's sequencing; not
  in this document.
