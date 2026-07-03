# Phase 1 Research: Orchestration Approach for Autonomous Trading System
**Agent:** AGENT 1 (business-product)
**Date:** 2026-06-07
**Branch:** develop

---

## Executive Summary

This document evaluates two candidate orchestration approaches for an autonomous trading system on top of the Moomoo brokerage (OpenD daemon + `moomoo-api` Python SDK):

- **HERMES PATH** — wrap Hermes Agent (Nous Research, MIT, 2026) as a long-running autonomous runtime with self-writing skills, cron scheduling, and persistent memory.
- **PYTHON PATH** — purpose-built deterministic Python application directly orchestrating `moomoo-api` + OpenD, with frozen-dataclass risk rules and hardware kill-switches.

**Bottom line:** The Python Path is clearly superior for this use case. The reasoning is developed in full below.

---

## Deliverable 1: Comparison Matrix

### 1A. Primary Candidates

| Dimension | Hermes Agent (HERMES PATH) | Custom Python App (PYTHON PATH) |
|---|---|---|
| **Setup effort** | Medium–High: install Hermes daemon, configure gateway, author Moomoo-specific skills from scratch, tune memory tiers | Low–Medium: compose `main.py` + `config.py` + `broker.py` on top of existing 94-script skill library already in repo |
| **Autonomy model** | High surface autonomy: self-writing skills, cron scheduler, chat-driven control via Telegram/Slack; BUT autonomy is LLM-controlled, introducing non-determinism in execution path | Explicit, deterministic: every code path is authored and auditable; Claude used only at signal-generation boundary |
| **Determinism** | ❌ Low: LLM inference governs runtime decisions; context compression causes drift after ~30 turns; self-modification of skill files | ✅ High: all risk rules in frozen-dataclass config; signal is the only AI-injected value; every other branch is code |
| **Self-improvement / memory** | ✅ Built-in: three-tier memory (USER.md + MEMORY.md hotload, SQLite FTS5 cross-session, optional vector store); auto-generates skills from repeated tasks | ❌ None by default: improvements require developer PR; knowledge is encoded in code and config, not accumulated at runtime |
| **Community + maintenance health** | 186k GitHub stars, 390+ contributors, last release v0.15.x (2026-06-05), rapid cadence (weekly releases), MIT *(retrieved 2026-06-07)* | N/A — you own the codebase; maintenance = your own dev capacity |
| **Moomoo/OpenD compatibility** | ❌ No native integration; requires authoring custom Moomoo skills that shell out to `moomoo-api` — possible but undocumented and untested at scale | ✅ Native: project already has 94 vendored Moomoo scripts, `common.py` env checks, `opend_ready.py` health gate; Python PATH composes directly |
| **Multi-asset: US stocks** | ✅ Possible via custom skills | ✅ Native (`US.AAPL` format, `get_portfolio`, `get_orders` scripts) |
| **Multi-asset: HK stocks** | ✅ Possible via custom skills | ✅ Native (`HK.00700`, trdmarket_auth detection in `server.py`) |
| **Multi-asset: Options** | ✅ Possible via custom skills | ✅ Native (62+ quote scripts incl. `get_option_chain`, `get_option_screen`, `get_option_volatility`) |
| **Multi-asset: Crypto** | ✅ Possible via custom skills | ✅ Native (`CC.BTCUSD`, `place_crypto_order`, `get_crypto_portfolio`, requires SDK ≥10.5.6508) |
| **Risk governance** | ❌ No financial authorization framework documented; no transaction caps, spending limits, or approval gates for financial operations *(source: aurpay.net security review, 2026-06-07)* | ✅ Full control: frozen-dataclass risk limits in `config/`; validation layer in `main.py`; kill-switches are just `if` statements |
| **Auditability / audit trail** | ❌ Poor: LLM reasoning not auditable; agent-generated skills may silently modify themselves across sessions | ✅ Full: every decision is deterministic code; logging of all order params + validation + API responses; git history |
| **Paper-trading default** | ⚠️ Requires explicit prompt engineering; nothing in Hermes enforces `TrdEnv.SIMULATE` — relies on skill authoring | ✅ Enforced in `common.py` and `server.py`; default is SIMULATE; live requires explicit `TRADING_ENV=LIVE` env var |

### 1B. Notable Alternatives

| Framework | Stars (2026-06-07) | Last Release | Setup Effort | Moomoo/OpenD Compat. | Multi-asset (US/HK/Options/Crypto) | Determinism | Notes |
|---|---|---|---|---|---|---|---|
| **NautilusTrader** | ~23k *(GitHub, 2026-06-07)* | v1.222+ (2026-05-18) | High: Rust-native engine, adapter required | ❌ No Moomoo adapter; 16 adapters (Binance, IB, Bybit, Kraken, OKX, Coinbase, dYdX, Hyperliquid, Polymarket, Databento, Tardis, others) | ✅ US/crypto; ⚠️ HK stocks = no native adapter; ✅ options (limited) | ✅ High: event-driven, deterministic backtester | High-quality engine; no HK stock or Moomoo support without custom adapter; steep learning curve; excellent for crypto/US multi-venue |
| **Freqtrade** | ~49.6k *(GitHub, 2026-06-07)* | 2026.4 (2026-04-30) | Low–Medium: Docker-based, well-documented | ❌ Crypto-only (CCXT); no stock or HK market support | ❌ US stocks = no; ❌ HK = no; ❌ options = no; ✅ crypto only | ✅ Strategy-level determinism | Not suitable: CCXT covers crypto exchanges only; excellent for crypto-only use cases with FreqAI ML integration |
| **QuantConnect / LEAN** | ~16k *(GitHub, 2026-06-07)* | Active (≤2 weeks ago) | High: C# core, Python strategies, local LEAN CLI | ❌ No Moomoo plugin; supported: IB, Alpaca, Coinbase, Kraken, Binance, Schwab, TradeStation, Tradier | ✅ US stocks/options; ⚠️ HK = not natively; ✅ crypto (via Coinbase/Binance/Kraken) | ✅ High | Cloud or local; no Moomoo adapter; C# dependency adds ops complexity; strong backtesting; HK stocks absent |
| **ib_insync-style custom** | N/A (library, not framework) | Active | Low: thin wrapper over IB TWS/Gateway | ❌ IB-specific; no Moomoo support | ✅ US stocks/options via IB; ❌ HK via IB limited | ✅ High | Analogous to the Python PATH but against Interactive Brokers instead of Moomoo; not applicable here |

**Key finding:** No major open-source trading framework natively integrates with Moomoo/Futu OpenD. Every alternative requires building a custom adapter or broker plugin. The Python PATH already sits on top of the native Moomoo Python SDK with 94 vendor-authored scripts — this is a structural advantage that none of the alternatives can replicate without significant engineering.

---

## Deliverable 2: Hermes Agent Capability Assessment

### 2A. Framework Overview

| Attribute | Detail | Verified? |
|---|---|---|
| Author | Nous Research (team behind Hermes, Nomos, Psyche model families) | ✅ Yes — github.com/NousResearch/hermes-agent |
| Release date | February 25, 2026 | ✅ Yes — multiple sources |
| Current version | v0.15.x (v2026.5.29.2), last release 2026-06-05 | ✅ Yes — GitHub releases |
| License | MIT | ✅ Yes — repo LICENSE |
| GitHub stars | 186k | ✅ Yes — GitHub, retrieved 2026-06-07 |
| Language | Python (82.9%), TypeScript (13.2%) | ✅ Yes — GitHub language bar |
| Self-reported maturity | "production-ready for personal workflows"; "review auto-generated skills before enabling" | ✅ Yes — official docs |

### 2B. Claimed Capabilities vs. Reality for This Use Case

#### Skill Writing
| Claim | Reality for Moomoo | Verdict |
|---|---|---|
| Auto-creates skills from repeated tasks | Hermes would need to be given (or generate from scratch) skills for `place_order`, `get_portfolio`, etc. using the `moomoo-api` SDK. The existing vendored scripts are in **Claude Code skill format** (`.md` docs + `.py` scripts), NOT Hermes skill format. Zero cross-compatibility documented. | ⚠️ Speculative — significant porting effort required |
| Skills stored in `~/.hermes/skills/` as Markdown | Agent-generated skills are "readable Markdown files" but carry skill-injection risk: "persistent prompt injection vectors across sessions" | ⚠️ Security concern for financial context |
| 40% token reduction with 20+ custom skills | Self-reported internal benchmark; not independently verified | ❓ Unverified |

#### Persistent Memory
| Tier | Mechanism | Trading Relevance |
|---|---|---|
| Tier 1 | `USER.md` + `MEMORY.md` hotloaded every session (guaranteed context) | Useful for strategy preferences, account IDs |
| Tier 2 | SQLite FTS5 cross-session recall with LLM summarization | Could store historical signal summaries |
| Tier 3 | Optional external vector store (mem0 etc.) | Overkill for this use case |
| **Critical gap** | **Context compression after ~30 turns silently removes older messages** — a risk parameter defined at turn 5 may be gone by turn 50 | ❌ Unacceptable for live risk governance |

#### Cron Scheduling
| Claim | Reality |
|---|---|
| Natural language cron via `/api/jobs` endpoint | Documented and present in v0.14+ | ✅ Documented |
| Delivery to 20+ messaging platforms | Documented | ✅ Documented |
| Suitable for market-hours trade loops | **Not documented for financial use cases** — general task scheduling only; no concept of market hours, exchange calendars, or trading halts | ⚠️ Would require custom skill authoring |

#### Chat-Driven Control (Telegram/Slack)
| Capability | Status |
|---|---|
| 20+ messaging platforms via unified gateway | ✅ Documented (Telegram, Discord, Slack, WhatsApp, Signal, Matrix, Teams, Email, Voice) |
| Send trade commands via Telegram | ✅ Architecturally possible via Hermes gateway |
| Approve/reject trades before execution | ❌ No financial authorization layer; approval gates are general-purpose only |
| Auditability of chat-driven commands | ❌ LLM interprets natural language commands — semantic drift is possible; a misspelled ticker or ambiguous instruction may be silently misinterpreted |

#### Moomoo OpenD Wrapping — How It Would Work
The only documented path is custom skill authoring:

```
User → Telegram/Slack → Hermes Agent → Custom Moomoo Skill (Markdown)
                                       → execute_code → Python subprocess
                                       → moomoo-api SDK → OpenD (127.0.0.1:11111)
                                       → Moomoo servers
```

This is **2 levels of indirection** above the native path. Each level adds:
- Latency (LLM inference + subprocess launch overhead)
- Non-determinism (LLM parsing of skill instructions)
- Attack surface (skill injection; unrestricted shell access in local backends)

**What is documented:** The `execute_code` tool, shell execution, custom skill creation.

**What is speculative:** Reliability of this chain for time-sensitive order execution; any Moomoo-specific skill in the Hermes ecosystem; financial approval gates; race condition handling; order status polling in a Hermes context.

### 2C. Hermes Financial Safety Assessment

| Risk Category | Finding | Source |
|---|---|---|
| No financial authorization framework | "For financial operations, there are none [controls]" — no transaction caps, spending limits, or approval gates specific to financial actions | aurpay.net security review, 2026-06-07 |
| Unrestricted shell access | Passes commands to bash without syscall restrictions; full filesystem read access including `.env`, SSH keys, and API credentials | aurpay.net, 2026-06-07 |
| Container approval bypass | Docker deployment "unconditionally skips" approval checks in testing — exactly how most CI/prod deployments would run | aurpay.net, 2026-06-07 |
| Self-modification of skills | Agent can overwrite its own skill files; a compromised session embeds persistent instructions affecting all future sessions | aurpay.net, 2026-06-07 |
| Context drift | Critical constraints defined early in a session may be silently dropped after context compression | AI Builder Club analysis, 2026-06-07 |
| Audit trail | LLM reasoning is not deterministically auditable; no immutable log of why a specific order was placed | Hermes docs (no audit trail mentioned) |

**Overall Hermes assessment for trading:** Hermes is a powerful general-purpose autonomous agent runtime that is genuinely impressive for developer productivity, personal automation, and agentic workflows. **It is architecturally inappropriate for autonomous financial execution** in its current form (v0.15.x). The gap is not a missing feature — it is a structural one: the framework was designed for trustless personal automation, not for the deterministic, auditable, risk-bounded execution that trading requires.

---

## Deliverable 3: Python App Fit Assessment

### 3A. Control and Determinism

The Python PATH is the natural continuation of what is already built. The existing codebase provides:

| Component | Status | Description |
|---|---|---|
| `skills/moomooapi/scripts/` | ✅ Done (94 scripts) | Complete market data, trading, and subscription API coverage |
| `skills/moomooapi/scripts/common.py` | ✅ Done | Env checks, `get_config()`, OpenD connectivity, version stamp, `ret_code == RET_OK` pattern |
| `dashboard/opend_ready.py` | ✅ Done | Exponential-backoff OpenD health gate — reusable in `main.py` |
| `dashboard/server.py` | ✅ Done | Reference architecture: allow-list enforcement, subprocess delegation, config precedence |
| `config/dashboard.config.example` | ✅ Done | Non-secret settings pattern; extends naturally to risk limits |
| `strategies/` | 🔲 To build | Stateless, testable signal modules |
| `config/` risk limits | 🔲 To build | Frozen-dataclass `RiskConfig` (max order size, daily loss, drawdown halt, position caps) |
| `main.py` | 🔲 To build | Orchestrator: OpenD gate → market data → strategy signals → validation → execution |
| `broker.py` | 🔲 To build | Thin Moomoo wrapper translating signals to `place_order.py` / `cancel_order.py` calls |

The **frozen-dataclass pattern** for risk limits is the right design because:
- Immutable at runtime: no code path can silently widen a limit
- Trivially testable: strategies run against mock data with fixed config
- Reviewable: any change to limits requires an explicit code diff, making "risk-limit values require explicit human review" enforceable by process

### 3B. Alignment with Existing Architecture

The CLAUDE.md architecture spec is already at ~80% of what a production-ready `main.py` needs:

```
main.py responsibilities (from CLAUDE.md):
1. is_opend_ready() exponential-backoff → already in opend_ready.py ✅
2. Load + validate config, log TRADING_ENV → dashboard pattern ✅
3. Init order: OpenD → Market Data → Account → Order Execution
4. Feed data to strategies, collect signals
5. Validation layer: max order size, daily loss, env routing
6. Graceful shutdown: cancel all open orders
```

None of this requires a third-party framework. The entire execution path from market data to order placement is already wired through the Moomoo SDK.

### 3C. Long-Run Maintainability

| Factor | Assessment |
|---|---|
| Dependency surface | Minimal: `moomoo-api`, `flask` (dashboard), standard library. No framework to keep up with. |
| Breaking change exposure | `moomoo-api` has a stable versioned SDK (pins in `requirements.txt`); skill bundle is vendored |
| Test surface | Strategies are stateless and mockable; risk validation is pure functions; no LLM calls to mock |
| Operational complexity | Single Python process + OpenD GUI. No daemon, no gateway, no agent runtime to manage. |
| Team bus factor | Code is explicit Python; any competent developer can read and maintain it without knowing the Hermes ecosystem |
| Upgrade path to fuller automation | Add strategies and config entries; the architecture already accommodates this by design |

---

## Deliverable 4: Time-to-First-Paper-Trade and Ongoing Maintenance

### 4A. HERMES PATH Timeline

**Assumptions:** Developer familiar with Python and Hermes basics; access to running OpenD; starting from zero Hermes skills for Moomoo.

| Phase | Duration | Work |
|---|---|---|
| Hermes setup & familiarization | 3–5 days | Install Hermes daemon, configure gateway, learn skill format, set up messaging platform |
| Author Moomoo skill bundle for Hermes | 1–2 weeks | Convert or re-author 5–10 core skills (accounts, portfolio, place_order, cancel_order, health check) in Hermes skill format; test each |
| Safety layer (manual, since Hermes has none) | 1–2 weeks | Build prompt-engineered approval gates; harden against context drift; add position-size enforcement via skill rules |
| Integration testing (paper) | 1–2 weeks | Test cron-driven signal → order flow end-to-end; validate paper trade placement and cancellation |
| Stability hardening | 1–3 weeks | Address context drift, self-modification risks, approval flow edge cases, Docker security isolation |
| **Total to first paper trade** | **6–12 weeks** | High variance due to Hermes novelty and financial-safety gap |

**Ongoing maintenance burden:** High
- Weekly Hermes releases (v0.13→v0.14→v0.15 in 6 weeks); breaking changes possible
- Skill drift: agent-generated skills accumulate and may subtly alter behavior
- Prompt engineering maintenance as models change
- Security patching: 8 P0 issues closed in v0.13, 12 P0 + 50 P1 in v0.14 — rapid churn in a framework managing your money
- No automated regression testing for LLM-driven decision paths

### 4B. PYTHON PATH Timeline

**Assumptions:** Developer familiar with Python; repo already cloned and OpenD running.

| Phase | Duration | Work |
|---|---|---|
| `config.py` with frozen-dataclass risk limits | 0.5 day | Define `RiskConfig`, `TradingConfig`, validation logic |
| `broker.py` Moomoo wrapper | 1–2 days | Thin class calling existing `place_order.py`, `cancel_order.py`, `get_portfolio.py` via `subprocess` (dashboard pattern) OR direct SDK import |
| One strategy (e.g., momentum signal) | 2–3 days | Stateless function; market data in → BUY/SELL/HOLD out; unit tests with mocked data |
| `main.py` orchestrator | 2–3 days | Wire `is_opend_ready()` → subscribe to market data → strategy loop → validate → broker.py |
| End-to-end paper trade test | 1–2 days | Place and cancel one order in `TrdEnv.SIMULATE`; verify fill handling |
| **Total to first paper trade** | **1–2 weeks** | Low variance: all Moomoo tooling already exists |

**Ongoing maintenance burden:** Low–Medium
- `moomoo-api` SDK updates (version-pinned; updates on own schedule)
- Strategy tuning: explicit code changes, git-tracked
- No LLM prompt maintenance
- Risk limit changes require deliberate code review (by design)
- Well-understood Python ecosystem; easy to add monitoring, alerts, CI

### 4C. Summary Table

| Metric | HERMES PATH | PYTHON PATH |
|---|---|---|
| Time to first paper trade | 6–12 weeks | 1–2 weeks |
| Ongoing maintenance (hrs/month) | High (15–30h) | Low–Medium (3–8h) |
| Risk of surprise production behavior | High (LLM non-determinism, context drift) | Low (deterministic code) |
| Incident recovery complexity | High (debug prompt chains, skill files, session state) | Low (read logs, fix code, redeploy) |

---

## Deliverable 5: Claude = Signals Only — Which Path Preserves the Separation?

### 5A. The Core Principle

> Claude generates trading signals (BUY/SELL/HOLD + confidence). Deterministic risk rules in code decide whether to act on the signal, what size to trade, and when to exit. Claude never has authority over the execution path.

This is the safety architecture that prevents the "hallucinating AI places a 100-share options order" failure mode.

### 5B. How Each Path Handles This

#### PYTHON PATH — Clean Separation ✅

```
Claude API call (signal generation)
    │
    ▼  returns: {signal: "BUY", ticker: "US.AAPL", confidence: 0.85}
    │
Validation layer in main.py (deterministic Python)
    │  checks: confidence ≥ threshold (frozen config)
    │          position already at max (frozen config)
    │          daily loss limit hit (frozen config)
    │          market hours (deterministic check)
    ▼
broker.py → place_order.py → moomoo-api SDK → OpenD → Moomoo
```

- Claude's output is a **data value** (signal dict), not a command
- Every decision downstream of the signal is a deterministic code path
- Claude cannot call `place_order.py` directly — only `main.py` can, and only after validation
- Risk limits are frozen dataclasses; Claude cannot change them at runtime
- Paper/live environment is determined by `TRADING_ENV` env var, not Claude's instruction

#### HERMES PATH — Blurred Separation ❌

```
User → Telegram/Slack
    │
    ▼
Hermes Agent (LLM-driven)
    │  interprets: "buy 10 shares of Apple if momentum is positive"
    │  generates: Moomoo skill call
    │  validates: via prompt-engineered rules (not deterministic code)
    ▼
Custom Moomoo skill → execute_code → place_order.py → moomoo-api
```

- The LLM is in the execution path, not just the signal path
- Risk rules are expressed in prompt text, which can drift, be compressed, or be overridden by creative interpretation
- Even if Claude is used only for signals, Hermes itself injects another LLM into the execution chain
- No structural guarantee that "Claude = signals only" is enforced — it depends on how skills are authored
- Self-modification of skills means risk rules could change between sessions without developer awareness

### 5C. Verdict

| Criterion | PYTHON PATH | HERMES PATH |
|---|---|---|
| Claude restricted to signal boundary | ✅ By architecture: signal is a data value passed to deterministic code | ⚠️ By convention only: depends on skill authoring discipline |
| Risk rules enforced at code level | ✅ Frozen dataclass; immutable at runtime | ❌ Expressed in prompt text; subject to drift |
| Execution path deterministic | ✅ Every branch is code | ❌ LLM inference at multiple points |
| Self-modification impossible | ✅ Code changes require git commit + review | ❌ Hermes can rewrite its own skill files |
| Audit trail of every decision | ✅ Structured logs at each pipeline stage | ❌ LLM reasoning is not reproducible |
| "Paper-first" enforced structurally | ✅ `TrdEnv.SIMULATE` default; live requires explicit env var | ⚠️ Depends on skill authoring |

**The Python Path structurally enforces the core principle. The Hermes Path can only approximate it through careful prompt engineering — and that approximation degrades silently over time.**

---

## Unverified Claims (Explicit)

| Claim | Status |
|---|---|
| Hermes Agent "40% token reduction with 20+ custom skills" | ❓ Self-reported internal benchmark; no independent reproduction |
| Hermes production stability for mission-critical workflows | ❓ Not independently verified; official docs qualify as "personal workflows"; financial use case = novel territory |
| Hermes Docker approval bypass (container deployment) | ⚠️ Reported by aurpay.net security review (2026-06-07); not independently reproduced — treat as credible concern pending Hermes team response |
| NautilusTrader HK stock support via custom adapter | ❓ Architecture supports adapters; HK stock adapter build effort not quantified |
| QuantConnect LEAN Moomoo broker plugin (community-built) | ❓ Not found in search; may exist as unreleased/unmaintained community work |
| Hermes v0.16 release (June 2026) | ❓ GitHub shows v0.15.x (v2026.5.29.2) as latest at time of research; v0.16 not yet released |

---

## Recommendation

**Proceed with the Python Path.**

The reasons are structural, not preferential:

1. **Moomoo/OpenD native support** — The existing 94-script skill bundle and `common.py` are the native interface. No framework bridges this gap.

2. **Determinism and auditability** — Trading systems that manage real money require reproducible behavior. The Python Path delivers this; Hermes cannot guarantee it.

3. **Core principle preserved** — The Python Path makes "Claude = signals only" enforceable by architecture. Hermes blurs this boundary by design.

4. **Dramatically faster to first paper trade** — 1–2 weeks vs. 6–12 weeks, with far lower ongoing maintenance burden.

5. **Risk governance** — Frozen-dataclass risk limits, explicit kill-switches, and the existing SIMULATE-by-default enforcement are already partially in place. Hermes has none of this for financial operations.

Hermes Agent is a genuinely impressive framework for autonomous personal AI workflows. Consider it for **non-execution tasks** where non-determinism is acceptable: research summaries, portfolio commentary, news digests routed to Telegram, or interactive strategy brainstorming. It should not be in the order execution path.

---

## Sources (retrieved 2026-06-07 unless noted)

- [Hermes Agent Official Site](https://hermes-agent.nousresearch.com/) — Nous Research
- [Hermes Agent GitHub](https://github.com/NousResearch/hermes-agent) — 186k stars, v0.15.x, MIT license
- [Hermes Agent Docs](https://hermes-agent.nousresearch.com/docs/) — Architecture, memory tiers, skill system, cron scheduling
- [Hermes Agent User Stories](https://hermes-agent.nousresearch.com/docs/user-stories) — Finance/trading use cases, architectural warnings
- [Hermes v0.15 Reference (blakecrosley.com)](https://blakecrosley.com/guides/hermes) — Velocity Release, skill bundles, Kanban, production maturity
- [Hermes Self-Hosted AI Overview (AI Builder Club)](https://www.aibuilderclub.com/blog/hermes-nous-research-self-improving-agent) — Memory model, skill lifecycle, community metrics
- [Hermes Security Risks (aurpay.net)](https://aurpay.net/aurspace/hermes-agent-security-risks-crypto-2026/) — Crypto/financial security gaps, shell access, container bypass
- [NautilusTrader GitHub](https://github.com/nautechsystems/nautilus_trader) — ~23k stars, Rust-native, adapters list
- [NautilusTrader Integrations](https://nautilustrader.io/docs/latest/integrations/) — 16 integrations; no Moomoo/Futu
- [Freqtrade GitHub](https://github.com/freqtrade/freqtrade) — 49.6k stars, v2026.4, crypto-only via CCXT
- [QuantConnect LEAN GitHub](https://github.com/QuantConnect/Lean) — 16k stars, C#/Python, no Moomoo adapter
- [QuantConnect Brokerages](https://www.quantconnect.com/brokerages) — Supported broker list
- [Moomoo OpenAPI Docs](https://openapi.moomoo.com/moomoo-api-doc/en/) — Protocol, OpenD architecture
- [moomoo-api PyPI](https://pypi.org/project/moomoo-api/) — SDK version history
- [WallTrading-Bot-MooMoo-Futu (GitHub)](https://github.com/LukeWang01/WallTrading-Bot-MooMoo-Futu) — Reference custom Python bot architecture on Moomoo
- [Moomoo API Skills Announcement (StockTitan)](https://www.stocktitan.net/news/FUTU/moomoo-launches-agentic-investing-with-introduction-of-moomoo-api-fvi247vymywb.html) — Moomoo's own AI agent integration announcement, April 2026
