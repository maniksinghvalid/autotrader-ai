"""Pure decoder for US equity option codes: US.{UNDER}{YYMMDD}{C|P}{strike*1000}.
Total — returns None for stock symbols or malformed codes, never raises. SDK-free."""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime
from typing import Optional

# Non-greedy underlying, then a 6-digit date, a C/P, and the strike*1000 integer.
_CODE_RE = re.compile(r"^(?P<under>.+?)(?P<ymd>\d{6})(?P<cp>[CP])(?P<strike>\d+)$")


@dataclass(frozen=True)
class ParsedOption:
    underlying: str
    expiry: date
    strike: float
    right: str          # "CALL" | "PUT"
    multiplier: int = 100


def parse_option_code(code: str) -> Optional[ParsedOption]:
    if not code:
        return None
    m = _CODE_RE.match(code)
    if not m:
        return None
    try:
        expiry = datetime.strptime(m.group("ymd"), "%y%m%d").date()
    except ValueError:
        return None
    right = "CALL" if m.group("cp") == "C" else "PUT"
    strike = int(m.group("strike")) / 1000.0
    return ParsedOption(underlying=m.group("under"), expiry=expiry,
                        strike=strike, right=right, multiplier=100)
