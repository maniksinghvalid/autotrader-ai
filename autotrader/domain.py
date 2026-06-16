"""Neutral domain types shared by the core. No Moomoo/SDK imports — this module
must import with no OpenD and no moomoo-api present."""
from __future__ import annotations

import enum
import math
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Literal, Mapping, Optional, Tuple

Side = Literal["BUY", "SELL"]
OrderType = Literal["MARKET", "LIMIT", "TRAILING_STOP"]


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

    def __post_init__(self):
        if self.side not in ("BUY", "SELL"):
            raise ValueError(f"OrderRequest.side must be BUY/SELL, got {self.side!r}")
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
    positions: Tuple[Position, ...] = ()

    def position_qty(self, symbol: str) -> int:
        for p in self.positions:
            if p.symbol == symbol:
                return p.qty
        return 0

    def gross_exposure(self) -> float:
        return sum(abs(p.qty) * p.avg_price for p in self.positions)
