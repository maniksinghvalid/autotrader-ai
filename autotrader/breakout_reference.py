"""Daily-cached strategy reference values (the internal strategies' only
stateful piece). Each reference computes its value once per trading day
(keyed by today_fn), retries on failure (a None result is NOT cached), and
never lets a data problem become a buy-at-open — None simply means the
strategy does not enter this tick.

Two references share one cache core so the fail-safe/warn-once/day-roll
mechanics cannot drift (same reason strategies share exits.py):
  * BreakoutReference — the N-day high (ref_high).
  * PullbackReference — the (M-day low, R-day SMA) pair, ALL-OR-NOTHING: if
    either leg is unavailable the whole value is None, so a missing SMA can
    never produce a half-gated entry.

Both expose kwargs(symbol) — the exact keyword arguments their strategy's
evaluate() expects — which is what lets TradeEngine.tick() stay
strategy-shape-agnostic."""
from __future__ import annotations

import logging
from datetime import date
from typing import Dict, Optional, Tuple

logger = logging.getLogger("autotrader.breakout")


class _DailyRefCache:
    """Once-per-day memo over an injected fetch. None is never cached (retried
    next tick) and warned once per day."""

    def __init__(self, fetch, describe: str, today_fn=None):
        self._fetch = fetch                  # symbol -> value | None
        self._describe = describe            # for the fail-safe warning
        self._today_fn = today_fn or date.today
        self._cache: Dict[Tuple[str, str], object] = {}   # (day_iso, symbol) -> value
        self._warned_day: Optional[str] = None

    def value(self, symbol: str):
        day = self._today_fn().isoformat()
        key = (day, symbol)
        if key in self._cache:
            return self._cache[key]
        v = self._fetch(symbol)
        if v is None:
            if self._warned_day != day:
                logger.warning("%s unavailable for %s today — strategy will not "
                               "enter until data is available (fail-safe)",
                               self._describe, symbol)
                self._warned_day = day
            return None                       # NOT cached -> retried next tick
        self._cache[key] = v
        return v


class BreakoutReference:
    def __init__(self, broker, lookback: int, today_fn=None):
        self._lookback = max(1, int(lookback))
        self._cache = _DailyRefCache(
            lambda symbol: broker.recent_high(symbol, self._lookback),
            f"breakout: {self._lookback}-day high", today_fn)

    def high(self, symbol: str) -> Optional[float]:
        v = self._cache.value(symbol)
        return float(v) if v is not None else None

    def kwargs(self, symbol: str) -> dict:
        return {"ref_high": self.high(symbol)}


class PullbackReference:
    def __init__(self, broker, low_lookback: int, sma_window: int, today_fn=None):
        self._m = max(1, int(low_lookback))
        self._r = max(1, int(sma_window))

        def fetch(symbol):
            lo = broker.recent_low(symbol, self._m)
            sm = broker.sma(symbol, self._r)
            if lo is None or sm is None:      # all-or-nothing fail-safe
                return None
            return (float(lo), float(sm))

        self._cache = _DailyRefCache(
            fetch, f"pullback: {self._m}-day low / {self._r}-day SMA", today_fn)

    def refs(self, symbol: str) -> Optional[Tuple[float, float]]:
        return self._cache.value(symbol)

    def kwargs(self, symbol: str) -> dict:
        v = self.refs(symbol)
        if v is None:
            return {"ref_low": None, "sma": None}
        return {"ref_low": v[0], "sma": v[1]}
