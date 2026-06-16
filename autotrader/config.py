"""Frozen risk configuration. Values come from env (RISK_* — typically sourced
from config/risk.config). NOTHING in here is hardcoded at a call site, and the
dataclass is immutable at runtime so the strategy/LLM layer cannot mutate a
limit (research R12). Changing a limit is a human-reviewed edit to the config."""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import FrozenSet


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
        allowed_overlays=overlays,
        max_option_contracts=int(_f("RISK_MAX_OPTION_CONTRACTS", 0)),
        max_option_premium_per_trade=_f("RISK_MAX_OPTION_PREMIUM_PER_TRADE", 0.0),
        option_target_delta=_f("RISK_OPTION_TARGET_DELTA", 0.30),
        option_dte_min=option_dte_min,
        option_dte_max=option_dte_max,
        option_dte_to_close=int(_f("RISK_OPTION_DTE_TO_CLOSE", 7)),
        option_profit_target_pct=_f("RISK_OPTION_PROFIT_TARGET_PCT", 0.5),
    )
