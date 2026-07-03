"""Pure helper: compute a capped marketable-limit price from a reference price.
No SDK, no I/O — importable with no OpenD. cap = max(bps term, tick-floor term);
the bps term governs normal names, the tick floor protects low-priced/thin names.
The cap is applied around the single reference price the Broker.get_quote contract
returns (see plan Investigation findings: true live bid/ask is not plumbed through
get_quote in v1)."""
from __future__ import annotations

from autotrader.config import RiskConfig
from autotrader.domain import Side


def tick_size(price: float) -> float:
    """US equity minimum price increment (two-tier ladder): sub-$1 names quote in
    1/100c, $1-and-up names in 1c. Sufficient for the allow-listed universe."""
    return 0.0001 if price < 1.0 else 0.01


def capped_limit_price(side: Side, ref_price: float, cfg: RiskConfig) -> float:
    """Marketable limit price: BUY = ref + cap, SELL = ref - cap, where
    cap = max(order_cap_bps/1e4 * ref, order_cap_ticks * tick). Rounded to the
    tick; never <= 0."""
    tick = tick_size(ref_price)
    cap = max(cfg.order_cap_bps / 1e4 * ref_price, cfg.order_cap_ticks * tick)
    raw = ref_price + cap if side == "BUY" else ref_price - cap
    # Round to the nearest tick; a SELL can never round to <= 0.
    px = round(raw / tick) * tick
    return px if px > 0 else tick
