"""Adapter: the daily *ticker sweep* JSON (emitted by the trade-routine skill)
-> canonical RoutineSignalPayload that the file-drop / webhook ingress accepts.

The ticker sweep speaks a different dialect than RoutineSignalPayload:
  sweep:    run_id, sweep_date, signal_changes[{ticker, from_signal, to_signal,
            composite_score, price, direction("upgrade"/"downgrade")}]
  canonical: routine_id, timestamp, signal_changes[{ticker, direction("UP"/"DOWN"),
            transition, points_delta, driver}]

Mapping (user-confirmed decisions; see the approved plan):
  D1  trade direction comes from the destination LABEL, not the up/down field:
      BUY/STRONG BUY -> UP (entry); CAUTION/AVOID -> DOWN (exit);
      HOLD/NEUTRAL (and unknown labels) -> skipped. So an "upgrade to NEUTRAL"
      never becomes a buy.
  D2  exits always clear the confidence filter: SELL gets a fixed high conviction
      (points_delta = -10 -> confidence 1.0). BUY conviction scales with the
      composite score (round(score/10), clamped 0..10).
  D3  bare tickers are qualified against the risk allow-list (CA.VDY vs US.VDY);
      unknown/ambiguous -> skipped with a warning.
  D4  the CLI drops the canonical payload into AUTOTRADER_SIGNAL_INBOX.

Imports stdlib + autotrader.signals only (schema, inbox helper) and the risk
config loader — NO flask, NO moomoo SDK. The webhook is left untouched."""
from __future__ import annotations

import json
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, FrozenSet, List, Optional

from autotrader.signals.schema import RoutineSignalPayload, SignalChange

logger = logging.getLogger("autotrader.signals.routine_adapter")

_BUY_LABELS = {"BUY", "STRONG BUY"}
_SELL_LABELS = {"CAUTION", "AVOID", "SELL"}
_SKIP_LABELS = {"HOLD", "NEUTRAL"}
_EXIT_POINTS = -10  # D2: confidence 1.0 -> exits always clear min_confidence


def _qualified_map(allowed_symbols: FrozenSet[str]) -> Dict[str, str]:
    """{bare_ticker: qualified} from the allow-list. A bare symbol present under
    more than one market (e.g. US.VDY and CA.VDY) maps to None -> ambiguous."""
    out: Dict[str, Optional[str]] = {}
    for qualified in allowed_symbols:
        q = qualified.strip().upper()
        bare = q.split(".", 1)[1] if "." in q else q
        out[bare] = None if bare in out and out[bare] != q else q
    return {b: q for b, q in out.items() if q is not None}


def _resolve_symbol(ticker: str, qmap: Dict[str, str]) -> Optional[str]:
    t = ticker.strip().upper()
    if "." in t:               # already qualified -> trust it (risk core re-checks)
        return t
    return qmap.get(t)         # None -> unknown or ambiguous -> skip


def _timestamp(run_id: str, sweep_date: str, fallback_ts: Optional[str]) -> str:
    """Prefer the HHMM embedded in 'routine-YYYYMMDD-HHMM-...'; else sweep_date at
    midnight; else the caller-supplied fallback (CLI passes 'now')."""
    parts = run_id.split("-")
    if len(parts) >= 3 and len(parts[1]) == 8 and len(parts[2]) == 4:
        try:
            return datetime.strptime(parts[1] + parts[2], "%Y%m%d%H%M").isoformat()
        except ValueError:
            pass
    if sweep_date:
        try:
            return datetime.strptime(sweep_date, "%Y-%m-%d").isoformat()
        except ValueError:
            pass
    if fallback_ts:
        return fallback_ts
    raise ValueError("cannot derive timestamp: unparseable run_id and no sweep_date")


def adapt_routine_sweep(raw: dict, *, allowed_symbols: FrozenSet[str],
                        fallback_ts: Optional[str] = None) -> RoutineSignalPayload:
    """Pure, deterministic conversion. Non-actionable entries (HOLD/NEUTRAL,
    unknown label, unresolved symbol) are dropped; the rest become SignalChanges."""
    run_id = str(raw.get("run_id") or raw.get("routine_id") or "").strip()
    if not run_id:
        raise ValueError("routine sweep is missing run_id/routine_id")
    sweep_date = str(raw.get("sweep_date") or "")
    qmap = _qualified_map(allowed_symbols)

    changes: List[SignalChange] = []
    for entry in raw.get("signal_changes", []):
        ticker = str(entry.get("ticker", "")).strip()
        to_signal = str(entry.get("to_signal", "")).strip().upper()
        from_signal = str(entry.get("from_signal", "")).strip().upper()
        score = int(entry.get("composite_score", 0))

        if to_signal in _BUY_LABELS:
            direction, points = "UP", max(0, min(10, round(score / 10)))
        elif to_signal in _SELL_LABELS:
            direction, points = "DOWN", _EXIT_POINTS
        else:  # HOLD / NEUTRAL / unknown -> not actionable
            if to_signal not in _SKIP_LABELS:
                logger.warning("skip %s: unknown to_signal %r", ticker, to_signal)
            else:
                logger.info("skip %s: %s is not actionable", ticker, to_signal)
            continue

        symbol = _resolve_symbol(ticker, qmap)
        if symbol is None:
            logger.warning("skip %s: not uniquely resolvable against allow-list", ticker)
            continue

        changes.append(SignalChange(
            ticker=symbol,
            direction=direction,
            transition=[s for s in (from_signal, to_signal) if s],
            points_delta=points,
            driver=f"routine ticker sweep (score {score})",
        ))

    return RoutineSignalPayload(
        routine_id=run_id,
        timestamp=_timestamp(run_id, sweep_date, fallback_ts),
        signal_changes=changes,
    )


def main(argv: Optional[List[str]] = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    argv = list(sys.argv[1:] if argv is None else argv)

    inbox_dir = os.getenv("AUTOTRADER_SIGNAL_INBOX")
    if not inbox_dir:
        logger.error("AUTOTRADER_SIGNAL_INBOX must be set (shared with the trader)")
        return 2

    src = argv[0] if argv else "-"
    try:
        text = sys.stdin.read() if src == "-" else \
            Path(os.path.expanduser(src)).read_text(encoding="utf-8")
        raw = json.loads(text)
    except (OSError, ValueError) as e:
        logger.error("cannot read/parse routine sweep %s: %s", src, e)
        return 1

    # local imports so the pure core above never pulls config/inbox at import time
    from autotrader.config import load_risk_config
    from autotrader.signals.inbox import atomic_write_bytes

    allowed = load_risk_config().allowed_symbols
    now_iso = datetime.now(timezone.utc).isoformat()
    try:
        payload = adapt_routine_sweep(raw, allowed_symbols=allowed, fallback_ts=now_iso)
    except ValueError as e:
        logger.error("invalid routine sweep: %s", e)
        return 1

    if not payload.signal_changes:
        logger.info("0 actionable signals in %s; nothing enqueued", src)
        return 0

    path = atomic_write_bytes(Path(os.path.expanduser(inbox_dir)),
                              payload.model_dump_json().encode("utf-8"))
    logger.info("enqueued %s (routine_id=%s, %d signal(s))",
                path.name, payload.routine_id, len(payload.signal_changes))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
