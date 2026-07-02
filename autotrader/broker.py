"""Broker contract. v1 ships a plain base class (NOT a Protocol yet — promoting
is a ~30-min mechanical refactor, research §4.2). Concrete brokers: SimBroker
(tests) and MoomooBroker (live OpenD). All Moomoo-isms live in MoomooBroker."""
from __future__ import annotations

from typing import List, Optional

from autotrader.domain import AccountSnapshot, Fill, OptionRight, OrderAck, OrderRequest
from autotrader.options.chain import OptionQuote


class Broker:
    def connect(self) -> None: raise NotImplementedError
    def is_ready(self) -> bool: raise NotImplementedError

    def heartbeat(self) -> bool:
        """Liveness probe for the watchdog. Defaults to is_ready(); MoomooBroker
        overrides with an active OpenD query."""
        return self.is_ready()

    def get_quote(self, symbol: str) -> Optional[float]: raise NotImplementedError

    def get_touch(self, symbol: str):
        """(bid, ask) for symbol, or None when unavailable. Base returns None
        so brokers without touch data degrade to last-quote pricing."""
        return None

    def get_option_chain(self, underlying: str, right: OptionRight,
                         dte_min: int = 0, dte_max: int = 100000) -> List["OptionQuote"]:
        """Return chain rows (strike/expiry/delta/premium) for one right, limited
        to expiries within [today+dte_min, today+dte_max]. Live impl is
        MoomooBroker; base raises so a broker without it fails loud."""
        raise NotImplementedError

    def place_order(self, req: OrderRequest) -> OrderAck: raise NotImplementedError
    def cancel_order(self, broker_order_id: str) -> None: raise NotImplementedError
    def cancel_all(self) -> None: raise NotImplementedError
    def get_account(self) -> AccountSnapshot: raise NotImplementedError
    def get_open_orders(self) -> Optional[List[OrderAck]]:
        """Working orders; None = the query FAILED (unknown book), [] = none."""
        raise NotImplementedError
    def reconcile_fills(self, since: Optional[str]) -> Optional[List[Fill]]:
        """Fills since `since`; None = the query FAILED, [] = none."""
        raise NotImplementedError
    def close(self) -> None: raise NotImplementedError
