"""Daily-cached N-day high for the internal breakout strategy. The only stateful
piece: computes the reference once per trading day (keyed by today_fn), retries on
failure (a None result is NOT cached), and never lets a data problem become a
buy-at-open — a None simply means the strategy does not enter this tick."""
from __future__ import annotations

import logging
from datetime import date
from typing import Dict, Optional, Tuple

logger = logging.getLogger("autotrader.breakout")


class BreakoutReference:
    def __init__(self, broker, lookback: int, today_fn=None):
        self._b = broker
        self._lookback = max(1, int(lookback))
        self._today_fn = today_fn or date.today
        self._cache: Dict[Tuple[str, str], float] = {}   # (day_iso, symbol) -> high
        self._warned_day: Optional[str] = None

    def high(self, symbol: str) -> Optional[float]:
        day = self._today_fn().isoformat()
        key = (day, symbol)
        if key in self._cache:
            return self._cache[key]
        h = self._b.recent_high(symbol, self._lookback)
        if h is None:
            if self._warned_day != day:
                logger.warning("breakout: no %d-day high for %s today — strategy will "
                               "not enter until data is available (fail-safe)",
                               self._lookback, symbol)
                self._warned_day = day
            return None                       # NOT cached -> retried next tick
        self._cache[key] = float(h)
        return self._cache[key]
