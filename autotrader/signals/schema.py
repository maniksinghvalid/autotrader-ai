"""Pydantic v2 schema for the external-signal ingress — Gemini's
RoutineSignalPayload, adopted from the bot design (roadmap §4: "the schema is
good"). Validated payloads are normalized to domain.Signal by normalize.py
BEFORE they ever reach the confidence filter + risk core. Imports pydantic
only — never the moomoo SDK."""
from __future__ import annotations

from datetime import datetime
from typing import Dict, List, Literal

from pydantic import BaseModel, Field


class SignalChange(BaseModel):
    ticker: str
    direction: Literal["UP", "DOWN"]
    transition: List[str] = Field(default_factory=list)
    points_delta: int
    driver: str = ""


class Catalyst(BaseModel):
    ticker: str
    event: str
    date: str
    value: float


class RoutineSignalPayload(BaseModel):
    routine_id: str
    timestamp: datetime
    signal_changes: List[SignalChange]
    # Validated and carried, but NOT executed in 2c (hard-stop execution and
    # catalyst logic are later phases — YAGNI).
    hard_stops: Dict[str, float] = Field(default_factory=dict)
    catalysts: List[Catalyst] = Field(default_factory=list)
