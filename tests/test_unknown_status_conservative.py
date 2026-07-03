"""V5b: an unmapped broker order status must surface as WORKING (UNKNOWN),
never silently drop off the book (which reads as 'filled'). Dropping an
UNKNOWN-status row would let a hedge look confirmed-filled, or let escalation
double-submit, when the order is actually still alive with a status this
codebase's mapping table doesn't recognize (CLAUDE.md: unrecognized statuses
are not successes).

Follows the offline fake-fixture pattern in tests/test_moomoo_broker_offline.py
(_Row/_DF/_broker_with_trade) rather than inventing a new mocking approach.
"""
from autotrader.domain import OrderState
from tests.test_moomoo_broker_offline import _Row, _DF, _broker_with_trade


class _UnknownStatusOrderListTrade:
    """order_list_query returns one row whose order_status is not in _STATUS_MAP."""
    def order_list_query(self, **kwargs):
        rows = [
            _Row(order_id="900001", code="US.AAPL", remark="cid-1",
                 order_status="SOME_NEW_STATUS"),
        ]
        return 0, _DF(rows)


def test_unknown_status_stays_in_working_orders():
    b = _broker_with_trade(_UnknownStatusOrderListTrade())
    orders = b.get_open_orders()
    assert len(orders) == 1
    assert orders[0].state is OrderState.UNKNOWN
