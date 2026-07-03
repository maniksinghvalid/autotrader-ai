"""Pure realized-P&L from fills using a running AVERAGE-COST basis per symbol
(not FIFO — matches Position.avg_price). DB-free: the reporter/runner passes
in fill rows. Never raises on ordinary inputs."""
from __future__ import annotations

from typing import Iterable, Optional, Tuple

FillRow = Tuple[str, str, float, float, str]  # (symbol, side, qty, price, ts)


def realized_from_fills(fills: Iterable[FillRow], day: str) -> Optional[float]:
    rows = sorted(fills, key=lambda r: r[4])
    held: dict = {}   # symbol -> shares held
    avg: dict = {}    # symbol -> running average cost
    realized = 0.0
    saw_sell_today = False
    for symbol, side, qty, price, ts in rows:
        q = float(qty)
        if side == "BUY":
            h = held.get(symbol, 0.0)
            a = avg.get(symbol, 0.0)
            new_h = h + q
            avg[symbol] = ((a * h) + (price * q)) / new_h if new_h else 0.0
            held[symbol] = new_h
        else:  # SELL
            a = avg.get(symbol, 0.0)
            if ts[:10] == day:
                realized += (price - a) * q
                saw_sell_today = True
            held[symbol] = held.get(symbol, 0.0) - q  # avg unchanged (avg-cost)
    return realized if saw_sell_today else None
