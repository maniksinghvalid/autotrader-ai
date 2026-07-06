"""Frozen risk configuration. Values come from env (RISK_* — typically sourced
from config/risk.config). NOTHING in here is hardcoded at a call site, and the
dataclass is immutable at runtime so the strategy/LLM layer cannot mutate a
limit (research R12). Changing a limit is a human-reviewed edit to the config."""
from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import date
from typing import FrozenSet, Optional

# NYSE full-day holidays for 2026 and 2027 — the shipped default for
# RISK_MARKET_HOLIDAYS. Operators must extend this via config each year
# (tracked in PRE-LIVE.md); holiday_horizon_warning() below flags when the
# calendar is about to run out so this doesn't silently degrade to "trades
# on holidays" (V9).
#
# 2027 dates independently verified against the official NYSE holiday
# calendar (New Year's Day, MLK Day, Presidents Day, Good Friday, Memorial
# Day, Juneteenth, Independence Day, Labor Day, Thanksgiving, Christmas),
# including Sat/Sun observance shifts: Juneteenth (Sat 6/19 -> observed
# Fri 6/18), Independence Day (Sun 7/4 -> observed Mon 7/5), and Christmas
# (Sat 12/25 -> observed Fri 12/24).
_DEFAULT_NYSE_HOLIDAYS = ("2026-01-01,2026-01-19,2026-02-16,2026-04-03,"
                          "2026-05-25,2026-06-19,2026-07-03,2026-09-07,"
                          "2026-11-26,2026-12-25,"
                          "2027-01-01,2027-01-18,2027-02-15,2027-03-26,"
                          "2027-05-31,2027-06-18,2027-07-05,2027-09-06,"
                          "2027-11-25,2027-12-24")


@dataclass(frozen=True)
class RiskConfig:
    trading_env: str            # "PAPER" | "LIVE" (LIVE also needs manual GUI unlock)
    min_confidence: float
    max_order_notional: float
    max_position_qty: int
    daily_loss_limit: float     # positive number; halt when day_pnl <= -limit
    max_gross_exposure: float
    allowed_symbols: FrozenSet[str]
    trailing_stop_pct: float = 0.0   # 0 disables broker-resting stops; e.g. 5.0 = 5%
    # Risk-per-trade position sizing (additive). risk_per_trade_pct is a FRACTION of
    # total_assets risked to the stop per BUY (0.01 = 1%); 0 disables -> fixed ORDER_QTY.
    risk_per_trade_pct: float = 0.0
    confidence_size_floor: float = 0.5   # size factor at confidence == min_confidence
    confidence_size_ceil: float = 1.0    # size factor at confidence == 1.0
    # Portfolio rebalancing (additive; rebalance_enabled default off). Drift band
    # is in percentage POINTS of weight; min_notional skips churn; cash_buffer is
    # reserved off investable equity; staleness skips stale target snapshots.
    rebalance_enabled: bool = False
    rebalance_band_pct: float = 5.0
    rebalance_min_notional: float = 200.0
    rebalance_cash_buffer_pct: float = 10.0
    target_staleness_hours: float = 24.0
    # Hard daily-loss flatten+halt threshold (must exceed the soft daily_loss_limit,
    # which gates new entries). Both are positive; breach when day_pnl <= -value.
    daily_loss_halt: float = 1000.0
    # Unrealized-drawdown ENTRY BLOCK (V4b): positive dollars; 0 disables. On
    # breach only NEW entries are blocked — never a flatten (open positions
    # exit via their trailing stops). Closes the "open position collapses
    # intraday, realized-only halt never fires" hole.
    unrealized_loss_gate: float = 0.0
    # --- Options overlays (additive; DEFAULT-OFF). allowed_overlays empty AND
    # max_option_contracts=0 both block option orders. Strike/expiry are chosen by
    # delta+DTE targets. All values are HUMAN-REVIEW risk limits. ---
    allowed_overlays: FrozenSet[str] = frozenset()
    max_option_contracts: int = 0
    max_option_premium_per_trade: float = 0.0
    option_target_delta: float = 0.30
    option_dte_min: int = 30
    option_dte_max: int = 45
    option_dte_to_close: int = 7        # exit declaration (enforced O4)
    option_profit_target_pct: float = 0.5
    # Contract count for strategies that are NOT share-covered (spread / diagonal /
    # LEAP). Share-covered overlays (covered call, collar) still size off held shares.
    option_default_contracts: int = 1
    # Fraction of NLV (snapshot.total_assets) at risk per option leg. Drives the
    # premium cap: debit legs cap paid premium at NLV*pct; credit legs cap collected
    # premium at NLV*pct/2 (200% stop => max loss 2x premium). max_option_premium_per_trade
    # remains an OPTIONAL absolute dollar ceiling (0 = off); the tighter of the two binds.
    option_max_risk_pct: float = 0.02
    # --- Protective limit orders (additive; DEFAULT-OFF => current MARKET behavior).
    # HUMAN-REVIEW risk params. When limit_orders_enabled is False the engine emits
    # the exact MARKET orders it does today. When True, entries/rebalance/option legs
    # are submitted as capped marketable limits: cap = max(order_cap_bps/1e4 * price,
    # order_cap_ticks * tick). Trailing-stop exits are unaffected. ---
    limit_orders_enabled: bool = False
    order_cap_bps: float = 0.0
    order_cap_ticks: float = 0.0
    # Dwell time given to each escalation stage (initial capped LIMIT, then the
    # re-peg) before it is judged "resting" and escalation advances. Zero dwell
    # would degenerate the feature into "MARKET with extra API calls" on live —
    # see _submit_with_escalation in main.py.
    escalation_dwell_seconds: float = 20.0
    # Trading calendar (spec W3): full-day market holidays, added to the
    # weekend gate in clock.is_trading_day. Not a risk limit — but
    # still config-only per CLAUDE.md (no hardcoded dates outside config).
    market_holidays: FrozenSet[date] = frozenset()
    # V11: account sovereignty. SOLE = today's behavior (sweep/flatten/report
    # the whole account). SHARED = every account-wide operation scopes to
    # AutoTrader's own tracked book (SNP-bot coexistence on the shared paper
    # account). LIVE+SHARED is structurally refused.
    account_ownership: str = "SOLE"
    # Stage-model cutover switch (V9): True = external signals (webhook/inbox)
    # are consumed. Stage 1 of the live cutover sets this False to disable
    # external signals while keeping the internal strategy + stops + halts
    # live; main() skips SignalInbox construction entirely when False.
    signals_enabled: bool = True


def _f(name: str, default: float) -> float:
    return float(os.getenv(name, str(default)))


def _b(name: str, default: bool) -> bool:
    return os.getenv(name, str(default)).strip().lower() in ("1", "true", "yes", "on")


def load_risk_config() -> RiskConfig:
    raw_syms = os.getenv("RISK_ALLOWED_SYMBOLS", "")
    symbols = frozenset(
        s.strip().upper() for s in raw_syms.split(",") if s.strip()
    )
    env = os.getenv("RISK_TRADING_ENV", "PAPER").strip().upper()
    if env not in ("PAPER", "LIVE"):
        env = "PAPER"
    raw_overlays = os.getenv("RISK_ALLOWED_OVERLAYS", "")
    overlays = frozenset(
        o.strip().upper() for o in raw_overlays.split(",") if o.strip()
    )
    option_dte_min = int(_f("RISK_OPTION_DTE_MIN", 30))
    option_dte_max = int(_f("RISK_OPTION_DTE_MAX", 45))
    if option_dte_min > option_dte_max:
        raise ValueError(
            f"RISK_OPTION_DTE_MIN ({option_dte_min}) must not exceed "
            f"RISK_OPTION_DTE_MAX ({option_dte_max})")
    daily_loss_limit = _f("RISK_DAILY_LOSS_LIMIT", 500)
    daily_loss_halt = _f("RISK_DAILY_LOSS_HALT", 1000)
    if daily_loss_halt <= daily_loss_limit:
        raise ValueError(
            f"RISK_DAILY_LOSS_HALT ({daily_loss_halt}) must exceed "
            f"RISK_DAILY_LOSS_LIMIT ({daily_loss_limit})")
    limit_orders_enabled = _b("RISK_LIMIT_ORDERS_ENABLED", False)
    order_cap_bps = _f("RISK_ORDER_CAP_BPS", 0.0)
    order_cap_ticks = _f("RISK_ORDER_CAP_TICKS", 0.0)
    if limit_orders_enabled and order_cap_bps <= 0 and order_cap_ticks <= 0:
        raise ValueError(
            "RISK_LIMIT_ORDERS_ENABLED=1 requires RISK_ORDER_CAP_BPS or "
            "RISK_ORDER_CAP_TICKS > 0 — zero caps degenerate to at-touch limits")
    raw_holidays = os.getenv("RISK_MARKET_HOLIDAYS", _DEFAULT_NYSE_HOLIDAYS)
    try:
        holidays = frozenset(date.fromisoformat(s.strip())
                             for s in raw_holidays.split(",") if s.strip())
    except ValueError as e:
        raise ValueError(f"RISK_MARKET_HOLIDAYS contains an invalid ISO date: {e}")
    ownership = os.getenv("RISK_ACCOUNT_OWNERSHIP", "SOLE").strip().upper()
    if ownership not in ("SOLE", "SHARED"):
        raise ValueError(f"RISK_ACCOUNT_OWNERSHIP must be SOLE or SHARED, got {ownership!r}")
    if env == "LIVE" and ownership == "SHARED":
        raise ValueError("RISK_TRADING_ENV=LIVE requires RISK_ACCOUNT_OWNERSHIP=SOLE, got "
                         "SHARED — live money never runs with scoped-down safety sweeps")
    return RiskConfig(
        trading_env=env,
        min_confidence=_f("RISK_MIN_CONFIDENCE", 0.6),
        max_order_notional=_f("RISK_MAX_ORDER_NOTIONAL", 2000),
        max_position_qty=int(_f("RISK_MAX_POSITION_QTY", 100)),
        daily_loss_limit=daily_loss_limit,
        max_gross_exposure=_f("RISK_MAX_GROSS_EXPOSURE", 50000),
        allowed_symbols=symbols,
        trailing_stop_pct=_f("RISK_TRAILING_STOP_PCT", 0.0),
        risk_per_trade_pct=_f("RISK_PER_TRADE_PCT", 0.0),
        confidence_size_floor=_f("RISK_CONFIDENCE_SIZE_FLOOR", 0.5),
        confidence_size_ceil=_f("RISK_CONFIDENCE_SIZE_CEIL", 1.0),
        rebalance_enabled=_b("RISK_REBALANCE_ENABLED", False),
        rebalance_band_pct=_f("RISK_REBALANCE_BAND_PCT", 5.0),
        rebalance_min_notional=_f("RISK_REBALANCE_MIN_NOTIONAL", 200.0),
        rebalance_cash_buffer_pct=_f("RISK_REBALANCE_CASH_BUFFER_PCT", 10.0),
        target_staleness_hours=_f("RISK_TARGET_STALENESS_HOURS", 24.0),
        daily_loss_halt=daily_loss_halt,
        unrealized_loss_gate=_f("RISK_UNREALIZED_LOSS_GATE", 0.0),
        allowed_overlays=overlays,
        max_option_contracts=int(_f("RISK_MAX_OPTION_CONTRACTS", 0)),
        max_option_premium_per_trade=_f("RISK_MAX_OPTION_PREMIUM_PER_TRADE", 0.0),
        option_target_delta=_f("RISK_OPTION_TARGET_DELTA", 0.30),
        option_dte_min=option_dte_min,
        option_dte_max=option_dte_max,
        option_dte_to_close=int(_f("RISK_OPTION_DTE_TO_CLOSE", 7)),
        option_profit_target_pct=_f("RISK_OPTION_PROFIT_TARGET_PCT", 0.5),
        option_default_contracts=int(_f("RISK_OPTION_DEFAULT_CONTRACTS", 1)),
        option_max_risk_pct=_f("RISK_OPTION_MAX_RISK_PCT", 0.02),
        limit_orders_enabled=limit_orders_enabled,
        order_cap_bps=order_cap_bps,
        order_cap_ticks=order_cap_ticks,
        escalation_dwell_seconds=_f("RISK_ESCALATION_DWELL_SECONDS", 20.0),
        market_holidays=holidays,
        account_ownership=ownership,
        signals_enabled=_b("RISK_SIGNALS_ENABLED", True),
    )


def holiday_horizon_warning(holidays: FrozenSet[date], today: date,
                            days: int = 60) -> Optional[str]:
    """Warn when the configured holiday calendar is about to run out — the
    silent failure mode is 'trades on holidays' (V9)."""
    if not holidays:
        return "RISK_MARKET_HOLIDAYS is EMPTY — holiday gating is off"
    horizon = (max(holidays) - today).days
    if horizon < days:
        return (f"RISK_MARKET_HOLIDAYS ends {max(holidays).isoformat()} "
                f"({horizon}d away) — extend the calendar before it expires")
    return None
