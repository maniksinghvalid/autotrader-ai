"""Lenient input coercion for the signal webhook.

The upstream cloud routine's deterministic payload builder is correct, but its
LLM agent intermittently bypasses it on the heavy daily sweep and hand-writes the
JSON, drifting onto alternate field names (seen in #portfolio-updates):

  top level:   run_id            -> routine_id
               sweep_date|generated_at -> timestamp
               signals           -> signal_changes   (when signal_changes is a
                                                       non-list, e.g. an int count)
  per change:  symbol|qualified_symbol -> ticker
               direction + magnitude  <- the DESTINATION label (D1/D2), NOT the
                                          source's up/down field; see below
               missing transition    -> build from prior_signal|prior + new_signal|signal
               driver                <- catalyst|event

The per-change mapping mirrors the canonical builder's user-confirmed contract in
routine_adapter.py, so the webhook and the file-drop adapter agree on what is a
trade (they must — both feed the same risk core):

  D1  direction + actionability come from the DESTINATION label, not the up/down
      field. BUY/STRONG BUY -> UP (entry); CAUTION/AVOID/SELL -> DOWN (exit);
      HOLD/NEUTRAL/unknown destination -> NON-actionable, dropped. So an
      "upgrade/downgrade to NEUTRAL" (the YNVDA case) never becomes a trade, and
      a BUY->HOLD is not a sell.
  D2  magnitude is NOT inferred from the source. BUY conviction scales with the
      composite score (round(score/10), clamped 0..10; no score -> 0, which is a
      no-trade, never a fabricated default). Exits get a fixed -10 (confidence 1.0,
      always clears the gate).

  Items with NO destination label but an already-canonical change object (explicit
  direction + explicit points_delta) are shape-normalized as-is — pure field
  mapping, no inference.

`coerce_payload` returns canonical JSON bytes. It never fabricates trade semantics:

  * It never invents the irreducible fields — a payload missing both
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
# Note: this is only consulted for the no-label canonical-passthrough path — when a
# destination label is present, D1 takes direction from the LABEL, not this field.
_DIRECTION_ALIASES = {
    "UP": "UP", "UPGRADE": "UP",
    "DOWN": "DOWN", "DOWNGRADE": "DOWN",
}

# D1 destination-label classes (mirrors routine_adapter._BUY_LABELS/_SELL_LABELS).
# Anything else as a destination (HOLD/NEUTRAL/unknown) is non-actionable -> dropped.
_BUY_LABELS = {"BUY", "STRONG BUY"}
_SELL_LABELS = {"CAUTION", "AVOID", "SELL"}
_EXIT_POINTS = -10  # D2: confidence 1.0 -> exits always clear min_confidence

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


def _coerce_change(it: Any) -> Tuple[Optional[Dict[str, Any]], bool]:
    """Map one raw change onto a canonical SignalChange dict per D1/D2.

    Returns (change | None, was_candidate). was_candidate is True only when the
    item was actionable but BROKEN (unsalvageable direction, missing ticker), so a
    None alongside it is a salvage failure the caller should reject. A benign skip
    — an unchanged row or a non-actionable HOLD/NEUTRAL destination — returns
    (None, False)."""
    if not isinstance(it, dict):
        return None, False

    raw_dir = str(it.get("direction", "")).strip().upper()
    explicit_dir = _DIRECTION_ALIASES.get(raw_dir)
    if raw_dir and explicit_dir is None:
        # An explicit but unrecognized direction (e.g. "SIDEWAYS") is unsalvageable —
        # don't reinterpret it from other fields.
        return None, True

    if it.get("changed") is False:
        return None, False  # source explicitly says "no change this sweep"

    prior_label = _label(it.get("prior_signal"), it.get("prior"))
    new_label = _label(it.get("new_signal"), it.get("signal"))
    score = _num(it.get("new_score"))
    if score is None:
        score = _num(it.get("score"))

    # D1/D2: the DESTINATION label decides direction, actionability, and magnitude.
    if new_label in _BUY_LABELS:
        direction = "UP"
        delta = max(0, min(10, int(round((score or 0.0) / 10))))  # D2; no score -> 0
    elif new_label in _SELL_LABELS:
        direction = "DOWN"
        delta = _EXIT_POINTS
    elif new_label is not None:
        # Explicit HOLD/NEUTRAL/other destination -> non-actionable, benign skip.
        return None, False
    else:
        # No destination label: accept only an already-canonical change object
        # (explicit direction + explicit points_delta). This is pure shape
        # normalization — no inference of trade intent.
        explicit_delta = _num(it.get("points_delta"))
        if explicit_dir is None or explicit_delta is None:
            return None, False
        direction = explicit_dir
        delta = int(round(explicit_delta))

    ticker = it.get("ticker") or it.get("symbol") or it.get("qualified_symbol")
    if not isinstance(ticker, str) or not ticker.strip():
        return None, True
    ticker = ticker.strip()

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
