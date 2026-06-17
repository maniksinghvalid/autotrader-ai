"""Lenient input coercion for the signal webhook.

The upstream cloud routine's deterministic payload builder is correct, but its
LLM agent intermittently bypasses it on the heavy daily sweep and hand-writes the
JSON, drifting onto alternate field names (seen in #portfolio-updates):

  top level:   run_id            -> routine_id
               sweep_date|generated_at -> timestamp
               signals           -> signal_changes   (when signal_changes is a
                                                       non-list, e.g. an int count)
  per change:  symbol|qualified_symbol -> ticker
               direction 'UPGRADE'/'DOWNGRADE'/'up'/'down' -> 'UP'/'DOWN'
               missing points_delta  -> derive from new_score-prior_score, else
                                        from a label-actionability default
               missing transition    -> build from prior_signal|prior + new_signal|signal
               driver                <- catalyst|event

`coerce_payload` maps those known shapes back onto the canonical schema's field
names and returns canonical JSON bytes. It is a SHAPE normalizer ONLY:

  * It never fabricates the irreducible fields — a payload missing both
    routine_id/run_id or any timestamp alias is left to fail (returns None).
  * An item that carries an EXPLICIT but unrecognized direction (e.g. "SIDEWAYS")
    is treated as a salvage failure, not silently reinterpreted.
  * The output is NOT trusted: webhook.py re-validates it with RoutineSignalPayload
    before enqueueing, so the receiver's invariant ("only validated
    RoutineSignalPayloads reach the inbox", CLAUDE.md) is preserved.

Imports stdlib only — no pydantic, no SDK."""
from __future__ import annotations

import json
from typing import Any, Dict, List, Optional, Tuple

# Explicit direction strings we accept and fold to the canonical enum. An explicit
# direction NOT in this map is an error we refuse to guess around (see _coerce_change).
_DIRECTION_ALIASES = {
    "UP": "UP", "UPGRADE": "UP",
    "DOWN": "DOWN", "DOWNGRADE": "DOWN",
}

# Higher = more bullish; used only to INFER a direction when none is provided.
_SIGNAL_RANK = {
    "STRONG BUY": 5, "BUY": 4, "HOLD": 3, "NEUTRAL": 2,
    "CAUTION": 1, "AVOID": 0, "SELL": 0,
}

# Labels that justify an actionable default magnitude (>0.6 confidence gate) when
# the source gave a direction but no numeric delta — mirrors the upstream builder,
# which only made BUY/SELL-class transitions actionable. Neutral-ish transitions
# get a sub-gate magnitude: accepted, but not auto-traded by the risk core.
_BUYISH = {"BUY", "STRONG BUY"}
_SELLISH = {"CAUTION", "AVOID", "SELL"}

# Mirrors domain.OverlayType / schema.SignalChange.overlay; unknown overlays are
# dropped (omitting is valid) rather than risking a validation failure.
_OVERLAYS = {
    "COVERED_CALL", "PROTECTIVE_PUT", "COLLAR",
    "CALL_DIAGONAL", "BEAR_PUT_SPREAD", "LEAP",
}


def _num(v: Any) -> Optional[float]:
    """A real number, or None. bool is explicitly excluded (it's an int subclass)."""
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    return None


def _label(*candidates: Any) -> Optional[str]:
    """First non-empty string candidate, upper-cased and stripped."""
    for v in candidates:
        if isinstance(v, str) and v.strip():
            return v.strip().upper()
    return None


def _default_delta(direction: str, new_label: Optional[str]) -> int:
    """Magnitude to use when the source gave a direction but no numeric delta."""
    if new_label in _SELLISH:
        return -10
    if new_label in _BUYISH:
        return 7
    return 3 if direction == "UP" else -3  # below the 0.6 gate: accepted, not traded


def _coerce_change(it: Any) -> Tuple[Optional[Dict[str, Any]], bool]:
    """Map one raw change onto a canonical SignalChange dict.

    Returns (change | None, was_candidate). was_candidate is True when the item
    looked like a real directional change, so a None alongside it is a salvage
    failure (the caller should reject) rather than a benign skip (e.g. an
    explicitly-unchanged row)."""
    if not isinstance(it, dict):
        return None, False

    raw_dir = str(it.get("direction", "")).strip().upper()
    explicit_dir = _DIRECTION_ALIASES.get(raw_dir)
    if raw_dir and explicit_dir is None:
        # An explicit but unrecognized direction (e.g. "SIDEWAYS") is unsalvageable —
        # don't reinterpret it from other fields.
        return None, True

    prior_label = _label(it.get("prior_signal"), it.get("prior"))
    new_label = _label(it.get("new_signal"), it.get("signal"))
    prior_score = _num(it.get("prior_score"))
    new_score = _num(it.get("new_score"))
    if new_score is None:
        new_score = _num(it.get("score"))
    explicit_delta = _num(it.get("points_delta"))
    changed_flag = it.get("changed")

    if changed_flag is False:
        return None, False  # source explicitly says "no change this sweep"

    label_change = (prior_label is not None and new_label is not None
                    and prior_label != new_label)
    is_candidate = (explicit_dir is not None or changed_flag is True
                    or explicit_delta is not None or label_change)
    if not is_candidate:
        return None, False

    # direction: explicit > score-delta sign > label-rank sign > delta sign
    direction = explicit_dir
    if direction is None and prior_score is not None and new_score is not None:
        diff = new_score - prior_score
        direction = "UP" if diff > 0 else ("DOWN" if diff < 0 else None)
    if direction is None and prior_label in _SIGNAL_RANK and new_label in _SIGNAL_RANK:
        diff = _SIGNAL_RANK[new_label] - _SIGNAL_RANK[prior_label]
        direction = "UP" if diff > 0 else ("DOWN" if diff < 0 else None)
    if direction is None and explicit_delta not in (None, 0):
        direction = "UP" if explicit_delta > 0 else "DOWN"
    if direction not in ("UP", "DOWN"):
        return None, True  # a candidate we could not resolve -> salvage failure

    ticker = it.get("ticker") or it.get("symbol") or it.get("qualified_symbol")
    if not isinstance(ticker, str) or not ticker.strip():
        return None, True
    ticker = ticker.strip()

    # points_delta: explicit > score delta > label-aware default. Never 0 (a 0 delta
    # normalizes to 0 confidence, i.e. a silent no-trade we didn't intend here).
    if explicit_delta is not None:
        delta = int(round(explicit_delta))
    elif prior_score is not None and new_score is not None:
        delta = int(round(new_score - prior_score))
    else:
        delta = _default_delta(direction, new_label)
    if delta == 0:
        delta = 1 if direction == "UP" else -1

    transition = it.get("transition")
    if not (isinstance(transition, list) and len(transition) == 2):
        transition = [prior_label or "", new_label or direction]

    change: Dict[str, Any] = {
        "ticker": ticker,
        "direction": direction,
        "transition": [str(transition[0]), str(transition[1])],
        "points_delta": delta,
        "driver": str(it.get("driver") or it.get("catalyst") or it.get("event") or ""),
    }
    overlay = it.get("overlay")
    if isinstance(overlay, str) and overlay.strip().upper() in _OVERLAYS:
        change["overlay"] = overlay.strip().upper()
    return change, True


def coerce_payload(raw: bytes) -> Optional[bytes]:
    """Return canonical RoutineSignalPayload JSON bytes for a recoverable drifted
    payload, or None when nothing recoverable is present (the caller then 400s).

    None is returned for: non-object/unparseable JSON; a missing routine_id (and
    run_id) or timestamp (and aliases); or a payload whose only change candidates
    could not be salvaged. An empty signal_changes is accepted ONLY when the source
    genuinely carried no change (canonical builders legitimately emit [])."""
    try:
        doc = json.loads(raw)
    except (ValueError, TypeError):
        return None
    if not isinstance(doc, dict):
        return None

    routine_id = doc.get("routine_id") or doc.get("run_id")
    timestamp = doc.get("timestamp") or doc.get("generated_at") or doc.get("sweep_date")
    if not isinstance(routine_id, str) or not routine_id.strip() or timestamp is None:
        return None  # the irreducible fields — we never invent them

    raw_changes = doc.get("signal_changes")
    if not isinstance(raw_changes, list):
        raw_changes = doc.get("signals")
    if not isinstance(raw_changes, list):
        raw_changes = []

    changes: List[Dict[str, Any]] = []
    salvage_failed = False
    for it in raw_changes:
        change, was_candidate = _coerce_change(it)
        if change is not None:
            changes.append(change)
        elif was_candidate:
            salvage_failed = True

    # Carried directional candidates but salvaged none -> a real failure, not an
    # empty (no-change) sweep. Let the caller reject it.
    if not changes and salvage_failed:
        return None

    canonical: Dict[str, Any] = {
        "routine_id": routine_id,
        "timestamp": timestamp,
        "signal_changes": changes,
    }
    hard_stops = doc.get("hard_stops")
    if isinstance(hard_stops, dict):
        canonical["hard_stops"] = hard_stops
    catalysts = doc.get("catalysts")
    if isinstance(catalysts, list):
        canonical["catalysts"] = catalysts
    portfolio_targets = doc.get("portfolio_targets")
    if isinstance(portfolio_targets, list):
        canonical["portfolio_targets"] = portfolio_targets

    return json.dumps(canonical, separators=(",", ":")).encode()
