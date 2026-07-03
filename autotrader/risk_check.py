"""Pure tiered intraday risk evaluation. No I/O, no SDK. day_pnl is negative when
losing. HALT (hard) is checked before GATE (soft) so the worse breach wins. The
engine applies the effect: GATE closes the entry gate; HALT flattens + halts."""
from __future__ import annotations

import enum

from autotrader.config import RiskConfig
from autotrader.domain import AccountSnapshot


class RiskAction(enum.Enum):
    OK = "OK"
    GATE = "GATE"     # close entry gate (no new BUYs); keep positions + stops
    HALT = "HALT"     # flatten all + cancel all + halt for the day


def evaluate(snapshot: AccountSnapshot, cfg: RiskConfig) -> RiskAction:
    if not snapshot.day_pnl_known:
        # Broker returned no P&L field. Unknown loss must never read as "no
        # loss": block new entries, keep positions + stops, never flatten.
        return RiskAction.GATE
    if snapshot.day_pnl <= -abs(cfg.daily_loss_halt):
        return RiskAction.HALT
    if snapshot.day_pnl <= -abs(cfg.daily_loss_limit):
        return RiskAction.GATE
    return RiskAction.OK
