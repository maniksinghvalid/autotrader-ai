"""Pydantic v2 schema for the external-signal ingress — Gemini's
RoutineSignalPayload, adopted from the bot design (roadmap §4: "the schema is
good"). Validated payloads are normalized to domain.Signal by normalize.py
BEFORE they ever reach the confidence filter + risk core. Imports pydantic
only — never the moomoo SDK."""
from __future__ import annotations

from datetime import datetime
from typing import Dict, List, Literal, Optional

from pydantic import BaseModel, Field


class SignalChange(BaseModel):
    ticker: str
    direction: Literal["UP", "DOWN"]
    transition: List[str] = Field(default_factory=list)
    points_delta: int
    driver: str = ""
    # Optional explicit option-overlay intent (D4). Absent => plain equity
    # (today's behavior). Literal mirrors domain.OverlayType so this module stays
    # pydantic-only; normalize.py converts the string to the enum.
    overlay: Optional[Literal[
        "COVERED_CALL", "PROTECTIVE_PUT", "COLLAR",
        "CALL_DIAGONAL", "BEAR_PUT_SPREAD", "LEAP",
    ]] = None


class Catalyst(BaseModel):
    ticker: str
    event: str
    date: str
    value: float


class TargetWeight(BaseModel):
    symbol: str
    score: float


class RoutineSignalPayload(BaseModel):
    routine_id: str
    timestamp: datetime
    signal_changes: List[SignalChange]
    # Validated and carried, but NOT executed in 2c (hard-stop execution and
    # catalyst logic are later phases — YAGNI).
    hard_stops: Dict[str, float] = Field(default_factory=dict)
    catalysts: List[Catalyst] = Field(default_factory=list)
    # Signal-score portfolio targets for the midday rebalancer (raw composite
    # scores; renormalized to weights at rebalance time). Optional — a payload
    # with only signal_changes is unchanged.
    portfolio_targets: List[TargetWeight] = Field(default_factory=list)
