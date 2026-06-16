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


def _f(name: str, default: float) -> float:
    return float(os.getenv(name, str(default)))


def load_risk_config() -> RiskConfig:
    raw_syms = os.getenv("RISK_ALLOWED_SYMBOLS", "")
    symbols = frozenset(
        s.strip().upper() for s in raw_syms.split(",") if s.strip()
    )
    env = os.getenv("RISK_TRADING_ENV", "PAPER").strip().upper()
    if env not in ("PAPER", "LIVE"):
        env = "PAPER"
    return RiskConfig(
        trading_env=env,
        min_confidence=_f("RISK_MIN_CONFIDENCE", 0.6),
        max_order_notional=_f("RISK_MAX_ORDER_NOTIONAL", 2000),
        max_position_qty=int(_f("RISK_MAX_POSITION_QTY", 100)),
        daily_loss_limit=_f("RISK_DAILY_LOSS_LIMIT", 500),
        max_gross_exposure=_f("RISK_MAX_GROSS_EXPOSURE", 50000),
        allowed_symbols=symbols,
        trailing_stop_pct=_f("RISK_TRAILING_STOP_PCT", 0.0),
        risk_per_trade_pct=_f("RISK_PER_TRADE_PCT", 0.0),
        confidence_size_floor=_f("RISK_CONFIDENCE_SIZE_FLOOR", 0.5),
        confidence_size_ceil=_f("RISK_CONFIDENCE_SIZE_CEIL", 1.0),
    )
