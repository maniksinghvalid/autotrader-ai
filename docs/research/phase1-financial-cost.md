# Phase 1 Financial Cost Analysis: Hermes Agent vs Custom Python App

**Agent:** financial-cost  
**Date:** 2026-06-07  
**Scope:** Cost and operational economics for an autonomous Moomoo trading system — Hermes Agent path vs Python-app path. All prices in USD unless noted. Claims are cited with source + retrieval date.  
**Modeling horizon:** 12 months at sustained activity (not a demo or burst estimate).

---

> **Critical Distinction:** "Hermes Agent" (the autonomous runtime framework, Nous Research, MIT license, ~Feb 2026) and "Hermes models" (NousResearch LLM fine-tunes: Hermes-2, Hermes-3, Hermes-4) are **separate products**. Hermes Agent does **not** exclusively or preferentially route through Hermes LLMs — it supports Claude, GPT, Gemini, DeepSeek, OpenRouter, and any OpenAI-compatible endpoint. This distinction is load-bearing for cost modeling: Hermes LLM models are not required for Hermes Agent, and the dominant ongoing cost in both paths is the Claude signal layer.

---

## Table of Contents
1. [Hermes-Path Costs](#1-hermes-path-costs)
2. [Python-Path Costs](#2-python-path-costs)
3. [Backend Costs — Moomoo OpenAPI Market Data](#3-backend-costs--moomoo-openapi-market-data)
4. [Claude Reasoning-Layer Cost](#4-claude-reasoning-layer-cost)
5. [12-Month TCO Comparison](#5-12-month-tco-comparison)
6. [Dominant Cost Line Analysis](#6-dominant-cost-line-analysis)
7. [Sources](#7-sources)

---

## 1. Hermes-Path Costs

### 1a. Hermes Agent Runtime Overview

Hermes Agent is an MIT-licensed open-source autonomous agent by Nous Research (~Feb 2026). It installs via a single `curl` command, stores persistent memory in `~/.hermes/` (SQLite + skill files), and ships a Hermes Desktop v0.15.2 GUI app (released 2026-06-02) alongside a headless server mode.

**Key capability for trading context:** After complex multi-step tasks, Hermes writes reusable "learned skills" — useful for encapsulating Moomoo quote/trade script invocations, but also a risk vector (see Section 6, hidden costs).

**Model endpoint routing (as of 2026-06-07):**

| Provider | Integration type | Notes |
|---|---|---|
| Nous Portal | Native OAuth (first-party) | Bundles 300+ models + tools (web, image, TTS, browser) |
| OpenRouter | API key | Aggregates 200+ models with automatic failover |
| Anthropic direct | OpenAI-compatible | Claude Opus/Sonnet/Haiku |
| OpenAI direct | Native | GPT-5.x family |
| Google direct | Native | Gemini 3.x family |
| Local Ollama / vLLM | OpenAI-compatible local endpoint | Zero per-token cost; GPU required |
| Any custom endpoint | `/v1/chat/completions`-compatible | Self-hosted vLLM, etc. |

Sources: [hermes-agent.org](https://hermes-agent.org/), [get-hermes.ai/models/](https://get-hermes.ai/models/) (retrieved 2026-06-07)

### 1b. Always-On VPS Hosting

The Hermes Agent runtime itself is lightweight. The LLM compute runs remotely via API unless self-hosted. However, the **Moomoo OpenD GUI** must also co-run on the same host — and on a headless Linux VPS it requires `Xvfb` (X virtual framebuffer), adding ~200-300 MB RAM overhead. Factor this into all tier selections below.

**Recommended target spec for co-running Hermes + OpenD:** 2-4 vCPU, 4-8 GB RAM, NVMe SSD

| Provider | Plan | vCPU | RAM | Storage | Monthly (USD) | Annual | Region | Notes |
|---|---|---|---|---|---|---|---|---|
| **Hetzner CX33** | Shared Intel | 4 | 8 GB | 160 GB NVMe | **~$10** | **~$120** | EU only | Cheapest overall; ~80-120ms added latency to US feeds |
| **Hetzner CX23** | Shared Intel | 2 | 4 GB | 40 GB NVMe | **~$8** | **~$96** | EU only | Minimum comfortable spec with OpenD + Xvfb |
| **Hetzner CPX31** | AMD EPYC | 4 | 8 GB | 160 GB NVMe | **$25.59** | **$307** | US-Ashburn | ✅ Best price/spec in a US datacenter — **recommended for US strategies** |
| **Vultr Regular** | AMD/Intel | 2 | 4 GB | 80 GB NVMe | **$20.00** | **$240** | US DCs | Clean pricing; straightforward billing |
| **Vultr Regular** | AMD/Intel | 4 | 8 GB | 160 GB NVMe | **$40.00** | **$480** | US DCs | More headroom |
| **DigitalOcean Basic** | Shared | 2 | 4 GB | 80 GB SSD | **$24.00** | **$288** | US DCs | Per-second billing since Jan 2026; 4 GB may be tight with Xvfb |
| **AWS Lightsail High Power** | — | 2 | 8 GB | 160 GB SSD | **$44.00** | **$528** | US | Flat-rate, static IP included; simple billing |
| **AWS t3.large** | EC2 On-Demand | 2 | 8 GB | EBS extra | **$60.74** | **$729** | US | 1-yr Savings Plan brings to ~$38/mo |
| **Local Mac/Linux** | Owned hardware | — | — | — | **$0** | **$0** | Local | Free but no HA; OpenD auth requires attention |

Sources: [hetzner.com/cloud](https://www.hetzner.com/cloud), [costgoat.com/pricing/hetzner](https://costgoat.com/pricing/hetzner), [vultr.com/pricing/](https://www.vultr.com/pricing/), [digitalocean.com/pricing/droplets](https://www.digitalocean.com/pricing/droplets), [aws.amazon.com/lightsail/pricing/](https://aws.amazon.com/lightsail/pricing/) (all retrieved 2026-06-07)

> **ARM caveat:** AWS t4g (Graviton ARM) is ~20% cheaper but requires confirming Moomoo publishes an ARM64 Linux OpenD binary. The Python SDK (`moomoo-api`) is architecture-agnostic but OpenD is a native binary.

> **Headless note:** Both Hermes and Python paths require the same OpenD GUI on VPS, meaning Xvfb overhead applies equally to both. This is **not a differentiator** between the two paths.

### 1c. Model API Costs — Hermes Orchestration Layer

Hermes introduces a **second model call layer** beyond the Claude signal-generation calls (Section 4). This layer handles task routing, memory management, and skill dispatch.

**Hermes orchestration call estimate:** ~15-20 calls/day at ~7,500 input + 500 output tokens/call

| Model (for orchestration) | Provider | $/M input | $/M output | Cost @ 20 calls/day | Annual |
|---|---|---|---|---|---|
| DeepSeek V4-Flash | DeepSeek / Nous Portal | $0.14 | $0.28 | ~$0.002/day | **~$0.73/yr** |
| Hermes-4 70B | DeepInfra | ~$0.20 avg | — | ~$0.015/day | **~$5.50/yr** |
| Hermes-3 70B | DeepInfra | $0.30 avg | — | ~$0.022/day | **~$8.03/yr** |
| Claude Haiku 4.5 | Anthropic | $1.00 | $5.00 | ~$0.075/day | **~$27/yr** |
| Claude Sonnet 4.6 | Anthropic | $3.00 | $15.00 | ~$0.225/day | **~$82/yr** |

> **Finding:** Hermes orchestration overhead is minimal ($0.73–$82/year) regardless of model chosen. The biggest cost lever is whether you use Claude Sonnet vs a cheap model for routing. Practically: use Haiku or DeepSeek for routing, Claude Sonnet for signals only.

### 1d. Hermes LLM Models (hermes-2, hermes-3, hermes-4) as Inference Models

These fine-tuned LLMs *can* be used as the agent's model backend. They are instruction-following / function-calling fine-tunes of Llama-3.1 — not finance-tuned.

| Model | DeepInfra | OpenRouter | Together AI | Fireworks AI |
|---|---|---|---|---|
| Hermes-2 Pro 8B | ~$0.030–0.040/M | ~$0.030–0.040/M | ❌ Not listed | ❌ Not listed |
| Hermes-3 70B | **$0.30/M** (in+out) | **$0.70/M** (in+out) | ❌ Not listed | ❌ Not listed |
| Hermes-3 405B | **$1.00/M** | **$1.00/M** | ❌ Not listed | ❌ Not listed |
| Hermes-4 70B | ~$0.13–0.30/M | $0.13 in / $0.40 out | ❌ Not listed | ❌ Not listed |

> **As of 2026-06-07: Together AI and Fireworks AI do not offer Hermes models in their standard catalogs.** Primary commercial hosts are DeepInfra (cheapest direct) and OpenRouter (with automatic failover). Hermes models are suitable for the orchestration layer but not recommended for signal generation where Claude's reasoning depth matters.

Sources: [artificialanalysis.ai/models/hermes-3-llama-3-1-70b/providers](https://artificialanalysis.ai/models/hermes-3-llama-3-1-70b/providers), [openrouter.ai/nousresearch/hermes-4-70b](https://openrouter.ai/nousresearch/hermes-4-70b), [openrouter.ai/nousresearch/hermes-3-llama-3.1-405b](https://openrouter.ai/nousresearch/hermes-3-llama-3.1-405b) (retrieved 2026-06-07)

### 1e. Nous Portal Subscription (Optional Add-On)

| Tier | Price/mo | Monthly usage credit | Net cost |
|---|---|---|---|
| Free | $0 | $0 (PAYG from $1) | PAYG only |
| Plus | $20 | $22 | Net ~$0 if credits consumed |
| Super | $100 | $110 | Net ~$0 if consumed |
| Ultra | $200 | $220 + highest rate limits | Net ~$0 if consumed |

Source: [portal.nousresearch.com/manage-subscription](https://portal.nousresearch.com/manage-subscription) (retrieved 2026-06-07)

> The Plus tier ($20/mo with $22 credit) is a wash if token spend fits within the credit. For most trading bots, direct API keys to Anthropic are simpler and cheaper at moderate volumes. Nous Portal is optional.

---

## 2. Python-Path Costs

### 2a. What the Python Path Entails

A purpose-built Python orchestrator that:
- Imports `moomoo-api` and wraps the existing `skills/moomooapi/` scripts
- Implements the `main.py` pipeline from `CLAUDE.md`: OpenD readiness gate → data → Claude signals → validation → execution
- Hard-codes risk rules (max order size, daily loss threshold, routing) in code — no agent autonomy
- Calls Claude for signal generation only (identical to Hermes path Claude cost)

### 2b. Development Effort Estimate

**Starting point:** This repo has the OpenD skill bundle (80+ scripts), `common.py`, and `dashboard/` as a reference architecture. These are reused — not rebuilt.

| Phase | Hours (low) | Hours (high) | Notes |
|---|---|---|---|
| Architecture & `main.py` scaffold | 8 | 12 | Per CLAUDE.md spec |
| OpenD readiness gate + config loading | 2 | 3 | `opend_ready.py` reused; extend for main loop |
| WebSocket subscription pipeline (`subscribe/`) | 10 | 15 | Real-time data feed to strategy |
| Strategy framework + 1 initial strategy | 15 | 20 | Stateless module; stop-loss + take-profit required |
| Validation layer (risk limits, kill-switches) | 10 | 15 | Daily loss cap, max order size, env routing |
| Order execution + status handling | 12 | 18 | All Moomoo order states; cancel-on-shutdown |
| Paper-env testing + mock data harness | 15 | 20 | Deterministic tests, no live OpenD required |
| Integration testing + debugging | 8 | 13 | End-to-end against paper account |
| **Total** | **80 hrs** | **116 hrs** | Median **~98 hrs** |

**At $150/hr opportunity cost:** $12,000 – $17,400 one-time  
**At $75/hr (salaried basis):** $6,000 – $8,700 one-time

### 2c. Python Path Infrastructure

**Identical to Hermes path** — same VPS options, same OpenD + Xvfb requirements. No runtime framework overhead.

**Differences vs Hermes:**
- No Hermes runtime (~200 MB less RAM pressure) — minimum spec could drop to Hetzner CX11 (~$5/mo)
- Strictly deterministic execution → no runaway agent loops
- No second model provider dependency (routing is Python code)
- Standard process monitoring (systemd, supervisord) vs agent-behavior monitoring

### 2d. Python Path Cost Summary

| Cost Item | Type | Monthly | Annual |
|---|---|---|---|
| VPS (Hetzner CPX31 US) | Recurring | $25.59 | $307 |
| Claude signal generation (24/day, Sonnet 4.6, cached) | Recurring | ~$16.63 | ~$199 |
| Moomoo market data | Recurring | $0† | $0† |
| Development (98 hrs median × $150/hr) | One-time | — | $14,700 |
| **Recurring ops total** | — | **$42.22** | **$506** |

†Currently free — see Section 3 for risk.

---

## 3. Backend Costs — Moomoo OpenAPI Market Data

Sources: [openapi.moomoo.com/moomoo-api-doc/en/intro/fee.html](https://openapi.moomoo.com/moomoo-api-doc/en/intro/fee.html), [openapi.moomoo.com/moomoo-api-doc/en/intro/authority.html](https://openapi.moomoo.com/moomoo-api-doc/en/intro/authority.html), [moomoo.com/us/feature/level2data](https://www.moomoo.com/us/feature/level2data), vendored `skills/moomooapi/docs/API_LIMITS.md` (all retrieved 2026-06-07)

### 3a. Market Data Fee Schedule

| Market | Data Level | Status | Details | If Promotion Ends |
|---|---|---|---|---|
| US Stocks | Level-1 (BBO) | ✅ **Free, permanent** | Nasdaq Basic; real-time | N/A |
| US Stocks | Level-2 (Nasdaq TotalView) | ⚠️ **Free (promotional)** | ~18% of US volume; 60 levels | Historically ~$25.99/mo |
| US Stocks | Level-2 (NYSE ArcaBook) | ⚠️ **Free (promotional)** | Non-Professional cert required | Price not published |
| US Options | OPRA Level-1 | ✅ **Free** (any US assets held) | Standard for active accounts | ~$2.99/mo *(unconfirmed, single source)* |
| Crypto | Spot mainstream | ⚠️ **Free (promotional)** | `CC.BTCUSD` format | Price not published |
| HK Stocks | Level-1 | ✅ **Free, permanent** | All global users | N/A |
| HK Stocks | Level-2 | ❌ **Paid** (non-mainland users) | Quotation card via in-app Data Store | Price in Data Store only; est. ~$5-9 USD/mo |
| HK SF Quotes | Full broker queue | ❌ **Paid** (all users) | Separate quotation card | Price in Data Store only |
| OpenD software | — | ✅ **Free, permanent** | No license fee | N/A |
| API trading | — | ✅ **Free, permanent** | No API surcharge vs app | N/A |

> **Promotional period risk:** US L2 and crypto free promotions have no stated end date but are explicitly labeled promotional. The Nasdaq TotalView promotion effective date was April 16 (per FUTU HK help center). Budget $50-75/month as a sensitivity case if all promotions end.

### 3b. API Rate Limits (Design Implications, Not Direct Costs)

Exceeding rate limits causes request failures — not overage charges. But they constrain system architecture:

| Endpoint | Limit | Design implication |
|---|---|---|
| `place_order` | 15 calls / 30s | Caps order velocity; not an $ concern |
| `order_list_query` | 10 calls / 30s | Dashboard: `refresh_seconds ≥ 20` rule applies to main loop too |
| `get_market_snapshot` | 60 calls / 30s (max 400 stocks/call) | Can monitor large universe cheaply via batching |
| Historical K-line | 60 calls / 30s | Historical backtesting throttled |

**Subscription quota tiers** (earned by account activity, not purchased):

| Account Tier | Subscription Quota | Option Quota |
|---|---|---|
| Basic (any account holder) | 100 symbols | 20 |
| ≥ 10,000 HKD assets | 300 symbols | 60 |
| > 500K HKD / 200+ orders/mo | 1,000 symbols | 200 |
| > 5M HKD / 2,000+ orders/mo | 2,000 symbols | 400 |

### 3c. Market Data Cost Summary

| Scenario | Monthly | Annual |
|---|---|---|
| US + Crypto only (promotions hold) | **$0** | **$0** |
| US + HK L2 + Crypto (promotions hold, HK L2 est.) | **~$7** | **~$84** |
| All US/Crypto promotions end, no HK L2 | **~$28** | **~$336** |
| All promotions end + HK L2 | **~$75** | **~$900** |
| **Base planning assumption** | **$0** | **$0** |

---

## 4. Claude Reasoning-Layer Cost

Prices from [platform.claude.com/docs/en/about-claude/pricing](https://platform.claude.com/docs/en/about-claude/pricing) and model overview (retrieved 2026-06-07).

### 4a. Current Anthropic API Pricing

| Model | Input ($/MTok) | Output ($/MTok) | Cache Read (0.1×) | Batch Input | Batch Output | Max Context |
|---|---|---|---|---|---|---|
| Claude Opus 4.8 | $5.00 | $25.00 | $0.50 | $2.50 | $12.50 | 1M tokens |
| Claude Opus 4.6 / 4.7 | $5.00 | $25.00 | $0.50 | $2.50 | $12.50 | 1M tokens |
| **Claude Sonnet 4.6** *(recommended)* | **$3.00** | **$15.00** | **$0.30** | **$1.50** | **$7.50** | **1M tokens** |
| Claude Haiku 4.5 | $1.00 | $5.00 | $0.10 | $0.50 | $2.50 | 200K tokens |

> **Tokenization note (Opus 4.7/4.8):** New tokenizer consumes up to 35% more tokens for identical text vs. older models. Effective cost may be higher than table prices suggest when migrating.

> **5-min cache write:** 1.25× input rate (cache write cost); pays back on second reuse. 1-hour cache write: 2.0× input rate; pays back after two re-uses.

### 4b. Per-Decision Token Budget Assumptions

| Component | Tokens | Cacheable? | Notes |
|---|---|---|---|
| System prompt + strategy definition | 2,000 | ✅ Yes | Static per session; 5-min cache |
| Market context (OHLCV for 5-10 instruments, indicators) | 3,000 | ✅ Partial | Mostly stable intra-session |
| Subscription history + account state | 1,500 | ⚠️ Partial | Rolling; some cache hits |
| Tool definitions (4 Moomoo quote tools) | 500 | ✅ Yes | Static |
| Recent news / sentiment | 1,500 | ❌ No | Fresh per decision |
| Portfolio state + open orders | 1,000 | ❌ No | Fresh per decision |
| **Total input per call** | **9,500** | 7,000 cacheable / 2,500 fresh | |
| **Output (signal + rationale)** | **500** | — | BUY/SELL/HOLD + price levels |

### 4c. Per-Decision Cost by Model

**Scenario A — No caching (all fresh input):**

| Model | Input cost (9.5K tokens) | Output cost (500 tokens) | **Cost/decision** |
|---|---|---|---|
| Claude Haiku 4.5 | $0.0095 | $0.0025 | **$0.0120** |
| Claude Sonnet 4.6 | $0.0285 | $0.0075 | **$0.0360** |
| Claude Opus 4.6 | $0.0475 | $0.0125 | **$0.0600** |
| Claude Opus 4.8 | $0.0475 + 35% tokenizer = ~$0.0641 | ~$0.0169 | **~$0.0810** |

**Scenario B — With 5-minute prompt cache (7,000 tokens cached @ 0.1× rate, 2,500 fresh):**

| Model | Cache read (7K × 0.1×) | Fresh input (2.5K × 1×) | Output (500) | **Cost/decision** |
|---|---|---|---|---|
| Claude Haiku 4.5 | $0.0007 | $0.0025 | $0.0025 | **$0.0057** |
| Claude Sonnet 4.6 | $0.0021 | $0.0075 | $0.0075 | **$0.0171** |
| Claude Opus 4.6 | $0.0035 | $0.0125 | $0.0125 | **$0.0285** |

> Cache savings: Sonnet 4.6 drops from $0.0360 → $0.0171/decision (52% reduction). At 24 calls/day, this is the difference between ~$314/year and ~$150/year.

### 4d. Annual Cost Projection by Cadence and Model

**Table A — Annual Cost, No Cache**

| Model | 1/day (365 calls) | 4/day (1,460) | **24/day (8,760)** | 96/day (35,040) |
|---|---|---|---|---|
| Haiku 4.5 | $4.38 | $17.52 | **$105.12** | $420.48 |
| Sonnet 4.6 | $13.14 | $52.56 | **$315.36** | $1,261.44 |
| Opus 4.6 | $21.90 | $87.60 | **$525.60** | $2,102.40 |

**Table B — Annual Cost, With 5-min Prompt Cache**

| Model | 1/day (365 calls) | 4/day (1,460) | **24/day (8,760)** | 96/day (35,040) |
|---|---|---|---|---|
| Haiku 4.5 | $2.08 | $8.32 | **$49.93** | $199.73 |
| **Sonnet 4.6** | **$6.24** | **$24.98** | **$149.80** | **$599.19** |
| Opus 4.6 | $10.40 | $41.61 | **$249.66** | $998.64 |

**Table C — Monthly Cost Summary (with cache), for budgeting**

| Model | 1/day | 4/day | **24/day** | 96/day |
|---|---|---|---|---|
| Haiku 4.5 | $0.17 | $0.69 | **$4.16** | $16.64 |
| Sonnet 4.6 | $0.52 | $2.08 | **$12.48** | $49.93 |
| Opus 4.6 | $0.87 | $3.47 | **$20.81** | $83.22 |

### 4e. Hybrid Model Strategy (Cost-Optimal)

Use Haiku 4.5 to screen 80% of candidates cheaply, then Sonnet 4.6 only for final signal on shortlisted instruments:

| Stage | Model | Calls/day | Cost/month |
|---|---|---|---|
| Screening (all instruments) | Haiku 4.5 | 96 | $16.64 |
| Final signal (top 20% through) | Sonnet 4.6 | 20 | $10.40 |
| **Blended total** | — | 116 | **$27.04** |

vs. all-Sonnet at 96/day = $49.93/month. **Hybrid saves ~46%.**

### 4f. Model Recommendation Summary

| Model | Best use case | Avoid if |
|---|---|---|
| **Haiku 4.5** | High-frequency screening, simple momentum signals | Multi-step reasoning needed (options strategy, macro) |
| **Sonnet 4.6** ✅ | Primary signal generation, cross-asset analysis | Budget extremely tight (use Haiku then) |
| **Opus 4.6/4.8** | Complex options strategy, once-daily macro research | Decision cadence > 4/day (cost compounds) |

---

## 5. 12-Month TCO Comparison

### 5a. Assumptions

| Parameter | Value |
|---|---|
| VPS | Hetzner CPX31 US-Ashburn ($25.59/month) |
| Claude model | Sonnet 4.6 with 5-min prompt cache |
| Decision cadence | **24/day** (hourly during US trading hours) — mid-cadence scenario |
| Market data | $0 base case (US L1 free; L2 + crypto promotional free) |
| Hermes orchestration model | Haiku 4.5 (pragmatic mid-tier; ~$27/year) |
| Developer opportunity cost | $150/hr (solo developer) |
| Paper trading environment year 1 |  `TrdEnv.SIMULATE` |

### 5b. Hermes Path — Year 1 Itemized TCO

| Cost Item | Type | Monthly | Annual | Notes |
|---|---|---|---|---|
| VPS hosting (Hetzner CPX31 US) | Recurring | $25.59 | $307 | Includes OpenD + Hermes runtime |
| Claude Sonnet 4.6 (24 signals/day, cached) | Recurring | $12.48 | $150 | Primary signal layer |
| Hermes orchestration (Haiku 4.5, 20 calls/day) | Recurring | $2.28 | $27 | Agent routing/memory layer |
| Moomoo market data | Recurring | $0 | $0 | Base case; promotional free |
| Nous Portal Plus (optional) | Recurring | $20.00 | $240 | Skip with direct API keys |
| Setup & Moomoo skill integration | One-time | — | $3,750 | 25 hrs × $150 opportunity cost |
| Ongoing maintenance (agent audit, skill drift) | Ongoing labor | ~$450 | $1,800 | ~2 hrs/mo × $75/hr effective |
| **Year 1 Total (cash only, no Portal, no labor)** | | **$40.35** | **$484** | |
| **Year 1 Total (cash + dev setup, no Portal)** | | — | **$4,234** | With 25-hr setup cost |
| **Year 1 Total (all-in incl. labor)** | | — | **$6,034** | Includes ongoing maintenance |

### 5c. Python Path — Year 1 Itemized TCO

| Cost Item | Type | Monthly | Annual | Notes |
|---|---|---|---|---|
| VPS hosting (Hetzner CPX31 US) | Recurring | $25.59 | $307 | Includes OpenD + Python bot |
| Claude Sonnet 4.6 (24 signals/day, cached) | Recurring | $12.48 | $150 | Same as Hermes path |
| Moomoo market data | Recurring | $0 | $0 | Same |
| Initial development (98 hrs median × $150) | One-time | — | $14,700 | Full `main.py` + strategies + tests |
| Ongoing maintenance (deterministic; auditable) | Ongoing labor | ~$75 | $450 | ~0.5 hrs/mo × $75/hr effective |
| **Year 1 Total (cash only, no labor)** | | **$38.07** | **$457** | |
| **Year 1 Total (cash + dev, no labor)** | | — | **$15,157** | With 98-hr development cost |
| **Year 1 Total (all-in incl. labor)** | | — | **$15,607** | Includes ongoing maintenance |

### 5d. Year 2+ Steady-State Annual Costs (Post-Amortization)

| Line Item | Hermes | Python |
|---|---|---|
| VPS | $307 | $307 |
| Claude Sonnet 4.6 | $150 | $150 |
| Hermes orchestration (Haiku) | $27 | $0 |
| Moomoo market data | $0 | $0 |
| Ongoing maintenance labor | $1,800 | $450 |
| **Annual total (incl. labor)** | **$2,284** | **$907** |
| **Annual total (cash only)** | **$484** | **$457** |

### 5e. Multi-Cadence Comparison Matrix

*(Sonnet 4.6, with cache, Year 1 all-in including dev and maintenance)*

| Cadence | Hermes Year 1 | Python Year 1 | Hermes Year 2+ | Python Year 2+ |
|---|---|---|---|---|
| 1 decision/day | $5,879 | $15,253 | $2,127 | $760 |
| 4 decisions/day | $5,933 | $15,282 | $2,160 | $760 |
| **24 decisions/day** | **$6,034** | **$15,607** | **$2,284** | **$907** |
| 96 decisions/day | $6,399 | $16,256 | $2,649 | $1,256 |

### 5f. Side-by-Side Summary

| Metric | Hermes Path | Python Path | Winner |
|---|---|---|---|
| Year 1 all-in cost | ~$6,034 | ~$15,607 | **Hermes (Year 1)** |
| Year 2+ annual all-in | ~$2,284/yr | ~$907/yr | **Python (Year 2+)** |
| Year 2+ cash cost (no labor) | ~$484/yr | ~$457/yr | **Tie / Python marginal** |
| Break-even (all-in) | — | Never — Python only wins if labor is free after Year 1 | *Context-dependent* |
| Cash-only break-even | — | Python wins from Year 2 ($457 vs $484) | **Python (Year 2+)** |
| Dev time investment | ~25 hrs | ~98 hrs | **Hermes** |
| Runaway cost risk | HIGH (agent loops) | LOW (deterministic) | **Python** |
| Maintenance burden | HIGH (non-determinism audit) | LOW (deterministic) | **Python** |

### 5g. Sensitivity: If Market Data Promotions End (+$75/mo to both paths)

Both paths bear this equally — it does not change the relative ranking. It adds $900/year to both.

---

## 6. Dominant Cost Line Analysis

### 6a. Where the Money Goes (Year 2+ Steady-State, 24 decisions/day)

#### Hermes Path

| Category | Annual (cash) | % of cash ops |
|---|---|---|
| VPS hosting | $307 | **63.4%** |
| Claude signal generation | $150 | **31.0%** |
| Hermes orchestration model | $27 | **5.6%** |
| Market data | $0 | 0% |
| **Total cash ops** | **$484** | 100% |

#### Python Path

| Category | Annual (cash) | % of cash ops |
|---|---|---|
| VPS hosting | $307 | **67.2%** |
| Claude signal generation | $150 | **32.8%** |
| Market data | $0 | 0% |
| **Total cash ops** | **$457** | 100% |

### 6b. How Cost Dominance Shifts with Decision Cadence

| Cadence | VPS % of cash ops | Claude % of cash ops |
|---|---|---|
| 1/day | ~98% | ~2% |
| 4/day | ~94% | ~6% |
| **24/day** | **67%** | **33%** |
| 96/day | **31%** | **69%** |

> **Inflection point: ~40-50 decisions/day is where Claude spend overtakes VPS spend.** Below that threshold, optimizing VPS selection matters more than model choice. Above it, model tier becomes the dominant cost lever.

### 6c. How Each Path Shifts the Cost Structure

| Factor | Hermes Path | Python Path |
|---|---|---|
| **Year 1 dominant cost** | Developer time (25 hrs × $150 = $3,750) — low absolute | Developer time (98 hrs × $150 = $14,700) — high absolute |
| **Year 2+ dominant cash cost** | VPS (63%) | VPS (67%) |
| **Year 2+ dominant all-in cost** | Maintenance labor ($1,800/yr) | Still maintenance labor (much lower: $450/yr) |
| **Model inference position** | ~31% of cash ops (two layers) | ~33% of cash ops (one layer) |
| **Cost variability risk** | HIGH — agent loops can multiply Claude calls 10-200× | LOW — Claude calls bounded by deterministic code |
| **Debugging labor cost** | HIGH — non-deterministic; agent memory state; skill drift | LOW — standard Python stack traces |

### 6d. Hidden / Tail-Risk Costs

#### Hermes Path Tail Risks

| Risk | Probability | Potential Cost Impact | Mitigation |
|---|---|---|---|
| **Runaway agent loop** (200× token spike in one session) | Medium | +$3–$300 per incident | Hard spend caps at Anthropic API key level; circuit-breaker wrapper |
| **Hermes learned-skill overwrites risk guardrails** | Low-Medium | Undefined (potential live order placement) | Keep deterministic risk core out-of-process; Hermes has no write access to execution layer |
| **OpenD restart → Hermes retries in loop** | Medium | Token spike + potential duplicate orders | External watchdog; idempotent order IDs in `place_order` calls |
| **Self-modified skill causes silent logic error** | Low | Missed/incorrect signals; undetected for days | Skill checksums; daily audit against golden state |
| **Model API price increase** (Anthropic 2× inference cost) | Low | +$150-$300/yr per layer | Model substitution plan; Haiku as fallback |
| **Debugging non-deterministic behavior** | Medium-High | $500–$2,000/incident in labor | Test harness that replays decisions against mock Moomoo |

#### Python Path Tail Risks

| Risk | Probability | Potential Cost Impact | Mitigation |
|---|---|---|---|
| **OpenD auth expires on VPS** (session timeout) | Medium | 1-2 hrs/incident to re-auth | Systemd watchdog + monitoring alert on health endpoint |
| **Strategy bug requires hotfix during market hours** | Medium | $300–$600/incident in labor | Extensive paper-env testing; test-driven development |
| **Paper → live transition validation** | Medium-High | 5-15 hrs for live safety audit | Staged rollout; CLAUDE.md defines this explicitly |
| **Claude API outage** | Low | Missed signals (no direct $ loss; bot defaults to HOLD) | Fallback HOLD signal in code |

### 6e. The Real Dominant Cost: Developer Time

At any decision cadence with Sonnet 4.6 and promotions holding, developer time completely dominates Year 1:

| Path | Year 1 Cash Ops | Year 1 Dev Cost | Dev % of Total Year 1 |
|---|---|---|---|
| Hermes | $484 | $3,750 | **89%** |
| Python | $457 | $14,700 | **97%** |

**Everything else — VPS, Claude, market data — is rounding error in Year 1.** The decision between paths is fundamentally a risk/labor tradeoff:
- **Hermes wins** if developer time is scarce, the team accepts non-deterministic agent behavior risk, and the ~25-hour setup is achievable
- **Python wins** if the team wants auditable deterministic execution, lower ongoing maintenance, and can invest the one-time development hours

### 6f. Recommendation Summary

| Criterion | Recommend |
|---|---|
| Minimize Year 1 cash outlay | **Either** — both under $500 cash |
| Minimize Year 1 all-in cost (incl. dev time) | **Hermes** ($6K vs $16K) |
| Minimize Year 2+ ongoing cost | **Python** ($907 vs $2,284 all-in; $457 vs $484 cash-only) |
| Minimize operational risk in production | **Python** |
| Fastest path to working paper-trading bot | **Hermes** |
| Best fit for sole developer with day job | **Hermes** (less upfront, more ongoing vigilance) |
| Best fit for funded team building production system | **Python** |

---

## 7. Tail-Cost & Worst-Case Appendix
*(Added post devils-advocate challenge, 2026-06-07)*

### 7a. Worst-Case Inference: Price Escalation × Cadence Creep

**Baseline (Section 4, Table B):** Sonnet 4.6 with cache, 24 decisions/day = $149.80/year per path.

**Scenario: cadence creeps to 96/day AND Anthropic prices rise.**

*Per-decision cost formula (cache model):*
`(7,000 cache tokens × input_price × 0.1 + 2,500 fresh tokens × input_price + 500 output tokens × output_price) / 1,000,000`

At **2× pricing** (Sonnet: $6.00 input / $30.00 output):
- Per decision: $0.0342 (vs $0.0171 baseline)

At **5× pricing** (Sonnet: $15.00 input / $75.00 output):
- Per decision: $0.0855 (vs $0.0171 baseline)

**Annual Claude signal cost — Python Path:**

| Cadence | 1× (current) | 2× price | 3× price | 5× price |
|---|---|---|---|---|
| 24/day | $149.80 | $299.60 | $449.40 | $749.00 |
| **96/day** | **$599.19** | **$1,198.38** | **$1,797.57** | **$2,995.96** |

**Annual Claude cost — Hermes Path** (adds orchestration layer; at 96/day, assume 40 orchestration calls/day at Haiku rates):

*Orchestration at 40 calls/day: 40 × 7,500 input + 40 × 500 output = 310,000 tokens/day*

| Cadence + pricing | Signal (Sonnet) | Orchestration (Haiku) | **Hermes total** |
|---|---|---|---|
| 24/day, 1× | $149.80 | $73/yr† | **$222.80** |
| 96/day, 1× | $599.19 | $146/yr | **$745.19** |
| 96/day, 2× | $1,198.38 | $292/yr | **$1,490.38** |
| 96/day, 5× | $2,995.96 | $730/yr | **$3,725.96** |

†Note: the $27/yr figure in Section 1c used a lower call estimate; corrected here at 20 calls/day × full token count = $73/yr.

**Worst-case annual inference delta vs baseline:**
- Python at 96/day + 5× price: **$2,996/yr** vs $150 baseline = **20× increase**
- Hermes at 96/day + 5× price: **$3,726/yr** vs $223 baseline = **17× increase**
- At this point, model inference ($3,726) exceeds VPS ($307) + market data ($0) combined by **12×**
- The cost ordering inverts entirely: inference becomes the dominant spend line

**Risk materialization probability:**
- 2× price scenario: ~25-35% over 3-year horizon (market pricing has historically fallen, but Anthropic has raised prices on some tiers/fast modes)
- 5× price scenario: ~5-10% (severe; would require major market shift)
- Cadence creep to 96/day: ~40-60% (easy to rationalize adding more asset coverage)
- Both 5× price + 96/day occurring together: ~2-6%
- **Recommended budget reserve:** model a 2× price increase as the planning case; treat 5× as a tail scenario requiring a model substitution plan (Haiku, or Hermes-4 70B via DeepInfra)

---

### 7b. Runaway Agent Loop — Uncapped Overnight Damage

**Scenario:** Hermes agent enters a retry/reasoning loop at 11pm ET; no one notices until 7am = **8 hours unsupervised.**

**Anthropic Sonnet 4.6 rate limits by tier** *(retrieved 2026-06-07 from docs.anthropic.com/en/api/rate-limits)*:

| Tier | Input TPM (ITPM) | Output TPM (OTPM) | RPM | Monthly spend cap |
|---|---|---|---|---|
| Tier 1 (default) | 30,000 | 8,000 | 50 | $500 |
| Tier 2 | 450,000 | 90,000 | 1,000 | $500 |
| Tier 3 | 800,000 | 160,000 | 2,000 | $1,000 |
| Tier 4 | 2,000,000 | 400,000 | 4,000 | $200,000 |

**Critical note:** Cached input tokens do **not** count toward ITPM. A runaway loop accumulates growing context (increasingly uncached), so this distinction matters less as the loop persists.

**Maximum theoretical token burn over 8 hours (480 minutes):**

| Tier | Uncached input tokens | Output tokens | Input cost @ Sonnet | Output cost | **8-hr max** |
|---|---|---|---|---|---|
| Tier 1 | 30K × 480 = 14.4M | 8K × 480 = 3.84M | $43.20 | $57.60 | **~$101** |
| Tier 2 | 450K × 480 = 216M | 90K × 480 = 43.2M | $648 | $648 | **~$1,296** |
| Tier 3 | 800K × 480 = 384M | 160K × 480 = 76.8M | $1,152 | $1,152 | **~$2,304** |

> **Which tier will this trading bot be on?** To handle real-time quote subscriptions + signal generation at 24+ decisions/day, Tier 2 is the realistic operational tier. A trading bot with automated quote calls + signal generation easily crosses the Tier 1 → Tier 2 threshold (Tier 2 requires a $40 cumulative credit purchase).

**Realistic 8-hour loop estimate at Tier 2:**

In practice, a runaway loop does not continuously hit ITPM maximums because API calls have latency (~2-5 seconds per round-trip). More realistic: 8-12 calls/minute.

| Call rate | Calls/8hrs | Tokens/call (growing context) | Avg tokens | Cost @ Sonnet |
|---|---|---|---|---|
| 8 calls/min | 3,840 | 25K input + 800 output (avg) | 96M input + 3M output | $288 + $46 = **$334** |
| 12 calls/min | 5,760 | 30K input + 800 output (avg) | 173M input + 4.6M output | $518 + $69 = **$587** |

**Realistic uncapped 8-hour Hermes loop cost at Tier 2: $334–$587.**

Theoretical maximum if rate limits are fully saturated at Tier 2: **$1,296**.

**The HARD STOP — and its limitations:**

| Mechanism | Stops at | Limitation |
|---|---|---|
| Anthropic rate limits (ITPM/OTPM) | Throttles throughput to tier ceiling | Does NOT stop spend; just slows the loop |
| Monthly spend cap (Settings > Limits) | Configured monthly maximum | **No daily limit exists** — entire monthly budget can be burned in hours |
| API credit balance exhaustion | When pre-paid credits run out | Depends on balance held |
| Workspace sub-limits (per-workspace ITPM/OTPM) | Configurable per workspace | **Cannot be set on the default workspace** |

**The only configurable hard dollar stop is the monthly spend cap in the Anthropic console.** There is no native daily cap.

**Practical failure scenario:** Monthly spend cap set to $200 (2× expected $100/month at moderate use). An 8-hour Tier 2 loop hits $334-$587 — **the monthly cap stops it mid-incident** but the entire month's legitimate trading budget is burned. The bot cannot make any more API calls until the next billing cycle unless the cap is manually raised.

**Recommended mitigations (both paths apply; Hermes has higher probability of triggering):**
1. Set Anthropic monthly cap to 3-5× expected monthly spend (buffer, not pinch)
2. Configure a separate API workspace for the trading bot with a **workspace-level ITPM sub-limit** (note: this requires a non-default workspace)
3. Implement a client-side circuit breaker using `anthropic-ratelimit-remaining-tokens` response headers
4. Set alerts on daily token spend via the Anthropic Console Usage page or billing webhooks

---

### 7c. Non-Determinism Debugging Labor — Revised Estimate

The original 2 hrs/month maintenance estimate conflated routine monitoring with incident response. For a system managing money, these are fundamentally different cost categories.

**Revised Hermes maintenance breakdown:**

| Category | Hours/month | Rate | Monthly cost | Annual cost |
|---|---|---|---|---|
| Routine monitoring (log review, skill health audit, decision spot-check) | 1.0 hr | $75/hr | $75 | $900 |
| Incident response (see table below) | variable | $75/hr | — | — |
| **Subtotal: routine** | | | **$75/mo** | **$900/yr** |

**Per-incident labor for non-reproducible bugs:**

The core problem: a financial system's non-deterministic incident cannot be replayed. The exact market data, agent memory state, and LLM sampling at decision time are gone. Reconstruction requires:

| Incident phase | Hours | Notes |
|---|---|---|
| Log aggregation + context reconstruction | 2–4 hrs | Which skill ran? What memory was active? What market data was retrieved? |
| Attempted reproduction (often impossible) | 0–8 hrs | 0 if clearly unreproducible; up to 8 if worth trying |
| Root cause hypothesis + verification | 3–6 hrs | Must reason about agent state without being able to reproduce it |
| Fix + validation against paper account | 2–4 hrs | How do you validate a fix for a bug you can't reproduce? |
| Trading decision audit (were any bad signals acted on?) | 2–4 hrs | Check order history, portfolio changes |
| Documentation + post-mortem | 1–2 hrs | |
| **Total per incident** | **10–28 hrs** | **At $75/hr: $750–$2,100** |

**At 2-3 incidents/year:**

| Scenario | Incidents | Hrs each | Total hrs | Annual labor cost |
|---|---|---|---|---|
| Low | 2 | 10 | 20 | **$1,500** |
| Mid | 2.5 | 16 | 40 | **$3,000** |
| High | 3 | 24 | 72 | **$5,400** |

**Revised Hermes maintenance labor total:**

| | Annual |
|---|---|
| Routine monitoring | $900 |
| Incident response (mid estimate) | $3,000 |
| **Total Hermes maintenance** | **$3,900/year** (was $1,800 — underestimated by 2.2×) |

**Revised Python maintenance labor:**

| Category | Hours/month | Annual labor |
|---|---|---|
| Routine monitoring (log review, alert check) | 0.5 hr | $450 |
| Incident response (1/year, deterministic, ~5 hrs) | — | $375 |
| **Total Python maintenance** | | **$825/year** (was $450 — underestimated by 1.8×) |

**Revised Year 2+ all-in annual TCO with corrected maintenance:**

| Path | VPS | Claude inference | Market data | Maintenance labor | **Annual total** |
|---|---|---|---|---|---|
| Hermes | $307 | $223 | $0 | $3,900 | **$4,430** |
| Python | $307 | $150 | $0 | $825 | **$1,282** |

**Hermes/Python labor ratio: 4.7× (range: 3×–7× depending on incident count)**

The original estimate of 4× was directionally correct but the absolute labor number for Hermes was underestimated; $3,900 vs $1,800.

---

### 7d. OpenD Downtime and Paper → Live Incident Response

#### OpenD Downtime: Direct Cost

**Paper trading (Year 1):** $0 direct financial loss. OpenD downtime during paper phase costs only recovery labor.

| Outage cause | MTTR (Python) | MTTR (Hermes) | Labor cost |
|---|---|---|---|
| VPS restart (auto) | ~2-5 min (systemd) | ~5-15 min | $0 |
| OpenD auth expiry (manual) | ~15-30 min | ~15-60 min (Hermes may retry) | $0-$37 |
| OpenD crash + core dump | ~30-60 min | ~30-90 min | $0-$75 |
| VPS provider incident | hours | hours | $0-$150 |

**Live trading:** OpenD downtime = inability to submit new orders OR execute stop-losses on existing positions.

| Scenario | Portfolio size | Position in motion | Outage duration | Missed exit cost |
|---|---|---|---|---|
| Conservative | $10,000 | $3,000 position, 1.5% adverse | 2 hrs | **$45** |
| Moderate | $20,000 | $7,000 position, 2% adverse | 2 hrs | **$140** |
| Aggressive | $50,000 | $15,000 position, 3% adverse | 4 hrs | **$450** |
| Options expiry during outage | $20,000 | 1 contract approaching expiry | Until resolved | **$200–$2,000** |

**Annual expected cost of OpenD outages in live trading:**
- Frequency: 3-6 incidents/year (VPS + auth + software events)
- Average cost per incident: $50–$300 (highly market-condition dependent)
- **Annual expected range: $150–$1,800** (wide variance; $0 in paper phase)

#### Paper → Live: First Incident Cost

The security scan flagged a concrete vulnerability (in `.remember/now.md`): **missing MASTER account check in `place_crypto_order.py` and `modify_order.py`**. This applies to both paths since both use the same underlying skill scripts.

**Incident probability and cost spectrum:**

| Scenario | Probability (Year 1 live) | Description | Trading loss | Labor cost | **Total** |
|---|---|---|---|---|---|
| **A: Minor** — duplicate order, quick cancel | 40% | Retry logic places same order twice; caught in minutes | $0–$100 | $150–$225 | **$150–$325** |
| **B: Moderate** — position sizing bug | 20% | 5-10× intended size placed; caught within hours | $100–$500 | $375–$600 | **$475–$1,100** |
| **C: Severe** — MASTER account gap triggered (unpatched) | 15% | Order lands on MASTER account; affects account group | $500–$2,000 | $1,500–$3,000 | **$2,000–$5,000** |
| **D: Catastrophic** — overnight accumulation, margin call | 3% | Bug + no circuit-breaker + adverse gap; margin called | $5,000–$20,000 | $2,000–$5,000 | **$7,000–$25,000** |
| No incident | 22% | Clean first year | $0 | $0 | $0 |

**Probability-weighted expected first-year live incident cost:**

`(0.40 × $237) + (0.20 × $787) + (0.15 × $3,500) + (0.03 × $16,000) + (0.22 × $0)`
`= $95 + $157 + $525 + $480 + $0 = **$1,257**`

**If MASTER account vulnerability ships unpatched:** Scenario C probability rises to ~35-45%, pushing expected cost to **$2,500–$4,200**.

**Additional Hermes-specific live incident risk:** Hermes "learned skills" could create a new execution path that bypasses the MASTER account check (or any other guardrail) if the agent autonomously modifies its Moomoo interaction skills. This adds an incremental risk vector absent in the Python path, which has no autonomous code-modification capability.

#### Summary: Tail Costs for Risk Register

| Risk | Python path | Hermes path |
|---|---|---|
| Worst-case inference (96/day, 5× price) | $2,996/yr | $3,726/yr |
| 8-hr overnight API loop at Tier 2 | Not applicable (no agent loop) | **$334–$1,296 per incident** |
| Non-determinism incident (2-3/year) | N/A (deterministic) | **$1,500–$5,400/yr** |
| OpenD downtime (live, annual) | $150–$1,800/yr | $150–$1,800/yr |
| First live incident (expected) | **$1,257** (both paths) | **$1,257 + agent bypass risk** |
| First live incident (MASTER gap unpatched) | **$2,500–$4,200** | **$3,000–$5,000** |
| **Worst-case Year 2 all-in** | **~$6,273** | **~$11,271** |

*Worst-case: 2× price + 96/day cadence + 3 incidents + one live incident. Both paths exclude catastrophic Scenario D.*

---

## 8. Sources

All retrieved 2026-06-07 unless noted.

| Source | URL | Data Used |
|---|---|---|
| Anthropic Claude Pricing (official) | `platform.claude.com/docs/en/about-claude/pricing` | All Claude model prices, batch, cache |
| Anthropic Model Overview | `platform.claude.com/docs/en/docs/about-claude/models/overview` | Model IDs, context lengths, tokenizer notes |
| Hermes Agent overview | `hermes-agent.org/about/` | Framework capabilities, architecture |
| Hermes Agent model providers | `get-hermes.ai/models/` | Supported endpoints + providers |
| Hermes Agent cost breakdown | `hostinger.com/tutorials/hermes-agent-cost` | Real monthly cost analysis |
| Nous Portal subscription | `portal.nousresearch.com/manage-subscription` | Tier prices + credits |
| Hermes-3 70B providers | `artificialanalysis.ai/models/hermes-3-llama-3-1-70b/providers` | DeepInfra $0.30/M price |
| OpenRouter — Hermes-3 70B | `openrouter.ai/nousresearch/hermes-3-llama-3.1-70b` | OpenRouter $0.70/M price |
| OpenRouter — Hermes-4 70B | `openrouter.ai/nousresearch/hermes-4-70b` | Hermes-4 pricing |
| OpenRouter — Hermes-3 405B | `openrouter.ai/nousresearch/hermes-3-llama-3.1-405b` | 405B $1.00/M parity |
| Hetzner Cloud pricing | `hetzner.com/cloud` | CX/CPX tier prices |
| Hetzner price adjustment notice | `hetzner.com/pressroom/statement-price-adjustment/` | Apr 2026 price adjustment |
| Hetzner USD prices (costgoat) | `costgoat.com/pricing/hetzner` | CPX31 US-Ashburn $25.59/mo |
| Vultr pricing | `vultr.com/pricing/` | Regular/High-Performance prices |
| DigitalOcean Droplets | `digitalocean.com/pricing/droplets` | Basic $24/mo; per-second billing note |
| AWS Lightsail pricing | `aws.amazon.com/lightsail/pricing/` | High Power $44/mo |
| AWS EC2 On-Demand | `aws.amazon.com/ec2/pricing/on-demand/` | t3.large $60.74/mo |
| Moomoo API Fees | `openapi.moomoo.com/moomoo-api-doc/en/intro/fee.html` | US/HK data fee structure |
| Moomoo API Authorities | `openapi.moomoo.com/moomoo-api-doc/en/intro/authority.html` | Subscription quotas + tiers |
| Moomoo US Level-2 feature page | `moomoo.com/us/feature/level2data` | US L2 free confirmation |
| Futu HK Help — Quote Right Adjustment | `futuhk.com/en/support/topic1_459` | L2 promotional grant details |
| Together AI pricing | `together.ai/pricing` | Hermes models not listed confirmation |
| Vendored API_LIMITS.md | `skills/moomooapi/docs/API_LIMITS.md` | Rate limits (10/30s order query, 60/30s snapshot, quota tiers) |
