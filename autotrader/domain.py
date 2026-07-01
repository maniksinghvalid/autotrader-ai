"""Neutral domain types shared by the core. No Moomoo/SDK imports — this module
must import with no OpenD and no moomoo-api present."""
from __future__ import annotations

import enum
import math
import re
from dataclasses import dataclass, field
from datetime import date
from types import MappingProxyType
from typing import Literal, Mapping, Optional, Tuple

Side = Literal["BUY", "SELL"]
OrderType = Literal["MARKET", "LIMIT", "TRAILING_STOP"]
OptionRight = Literal["CALL", "PUT"]
PositionEffect = Literal["OPEN", "CLOSE"]

# Suffix of a moomoo option code after the underlying prefix, e.g. for
# "US.AAPL260717C210000" after stripping "US.AAPL" → "260717C210000".
# Group 1 is "C" or "P". The leading \d{6} also guards prefix collisions
# (US.OXY…'s residual "XY26…" fails to match, so it is not counted for US.O).
_OPT_SUFFIX_RE = re.compile(r"^\d{6}([CP])\d+$")


class OverlayType(enum.Enum):
    """Full option-strategy range. O1 implements COVERED_CALL + PROTECTIVE_PUT;
    the rest are declared so the schema/enum never needs to change to add them —
    the planner rejects any value with no registry entry (SKIP_UNSUPPORTED_OVERLAY)."""
    COVERED_CALL = "COVERED_CALL"
    PROTECTIVE_PUT = "PROTECTIVE_PUT"
    COLLAR = "COLLAR"
    CALL_DIAGONAL = "CALL_DIAGONAL"
    BEAR_PUT_SPREAD = "BEAR_PUT_SPREAD"
    LEAP = "LEAP"


@dataclass(frozen=True)
class OptionContract:
    """A concrete tradable option. `code` is the moomoo option code (e.g.
    US.AAPL260717C200000). `multiplier` is shares per contract (US equity opts = 100)."""
    underlying: str
    expiry: date
    strike: float
    right: OptionRight
    code: str
    multiplier: int = 100

    def __post_init__(self):
        if self.right not in ("CALL", "PUT"):
            raise ValueError(f"OptionContract.right must be CALL/PUT, got {self.right!r}")
        if not self.underlying:
            raise ValueError("underlying must not be empty")
        if not self.code:
            raise ValueError("code must not be empty")
        if not math.isfinite(self.strike) or self.strike <= 0:
            raise ValueError(f"strike must be positive finite, got {self.strike}")
        if self.multiplier <= 0:
            raise ValueError(f"multiplier must be > 0, got {self.multiplier}")


class OrderState(enum.Enum):
    PENDING = "PENDING"
    SUBMITTED = "SUBMITTED"
    PARTIAL = "PARTIAL"
    FILLED = "FILLED"
    CANCELLED = "CANCELLED"
    REJECTED = "REJECTED"
    UNKNOWN = "UNKNOWN"  # ACK timeout / socket drop — NEVER treat as success (E2/R8)

    def is_success(self) -> bool:
        return self is OrderState.FILLED


class BrokerErrorKind(enum.Enum):
    NOT_READY = "NOT_READY"
    RATE_LIMIT = "RATE_LIMIT"
    REJECTED = "REJECTED"
    TIMEOUT = "TIMEOUT"
    UNSUPPORTED = "UNSUPPORTED"
    UNKNOWN = "UNKNOWN"


class BrokerError(Exception):
    def __init__(self, kind: BrokerErrorKind, message: str):
        super().__init__(f"[{kind.value}] {message}")
        self.kind = kind


@dataclass(frozen=True)
class Signal:
    symbol: str
    direction: Side
    confidence: float
    rationale: str
    stop_price: Optional[float] = None   # per-signal hard stop; None = none supplied
    overlay: Optional[OverlayType] = None   # None = plain equity signal (default)

    def __post_init__(self):
        if self.direction not in ("BUY", "SELL"):
            raise ValueError(f"Signal.direction must be BUY/SELL, got {self.direction}")
        if not (0.0 <= self.confidence <= 1.0):
            raise ValueError(f"confidence must be in [0,1], got {self.confidence}")
        if self.stop_price is not None and (not math.isfinite(self.stop_price)
                                            or self.stop_price <= 0):
            raise ValueError(f"stop_price must be a positive finite price, got {self.stop_price}")


@dataclass(frozen=True)
class OrderRequest:
    symbol: str
    side: Side
    qty: int
    order_type: OrderType
    limit_price: Optional[float]
    client_order_id: str
    trail_percent: Optional[float] = None   # required for TRAILING_STOP; else None
    option: Optional[OptionContract] = None    # None = equity order (default)
    position_effect: PositionEffect = "OPEN"     # OPEN/CLOSE (close logic: O4)
    correlation_id: Optional[str] = None         # groups legs of one overlay (multi-leg: O2)

    def __post_init__(self):
        if self.side not in ("BUY", "SELL"):
            raise ValueError(f"OrderRequest.side must be BUY/SELL, got {self.side!r}")
        if self.position_effect not in ("OPEN", "CLOSE"):
            raise ValueError(f"position_effect must be OPEN/CLOSE, got {self.position_effect!r}")
        if self.qty <= 0:
            raise ValueError("OrderRequest.qty must be > 0")
        if self.order_type == "LIMIT" and self.limit_price is None:
            raise ValueError("LIMIT order requires limit_price")
        if self.order_type == "TRAILING_STOP":
            if self.trail_percent is None or self.trail_percent <= 0:
                raise ValueError("TRAILING_STOP order requires trail_percent > 0")
            if self.limit_price is not None:
                raise ValueError("TRAILING_STOP order must not set limit_price")


@dataclass(frozen=True)
class OrderAck:
    client_order_id: str
    broker_order_id: Optional[str]
    state: OrderState
    raw: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self):
        # Freeze the raw envelope so the value object is genuinely immutable.
        object.__setattr__(self, "raw", MappingProxyType(dict(self.raw)))


@dataclass(frozen=True)
class Fill:
    fill_id: str
    symbol: str
    side: Side
    qty: float
    price: float
    ts: str


@dataclass(frozen=True)
class Position:
    symbol: str
    qty: int
    avg_price: float


@dataclass(frozen=True)
class AccountSnapshot:
    cash: float
    total_assets: float
    day_pnl: float
    stale: bool
    positions_loaded: bool = True   # False = position query FAILED (not flat)
    unrealized_pnl: float = 0.0
    positions: Tuple[Position, ...] = ()

    def position_qty(self, symbol: str) -> int:
        for p in self.positions:
            if p.symbol == symbol:
                return p.qty
        return 0

    def short_option_contracts(self, underlying: str, right: str) -> int:
        """Total open SHORT option contracts (sum of -qty over negative-qty
        positions) on `underlying` for the given right ("CALL"/"PUT"), parsed
        from moomoo option codes (e.g. US.AAPL260717C210000). Used both to
        reserve shares pledged to covered calls (so a trim never strips cover)
        and to bound stacked covered shorts in the risk core."""
        want = "C" if right == "CALL" else "P"
        total = 0
        for p in self.positions:
            if p.qty >= 0 or not p.symbol.startswith(underlying):
                continue
            m = _OPT_SUFFIX_RE.match(p.symbol[len(underlying):])
            if m and m.group(1) == want:
                total += -p.qty
        return total

    def gross_exposure(self) -> float:
        return sum(abs(p.qty) * p.avg_price for p in self.positions)
