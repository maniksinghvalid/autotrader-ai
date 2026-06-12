"""R19: place_crypto_order and modify_order must reject MASTER accounts.

We import the script functions and stub create_*_trade_context with a fake ctx
whose get_acc_list() returns a MASTER row, then assert the function exits(1)
BEFORE any order method is called.
"""
import os
import sys
import types
import pytest

TRADE_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "skills", "moomooapi", "scripts", "trade",
)


class _FakeDF:
    """Minimal pandas-like frame: one MASTER account row."""
    def __init__(self, rows):
        self._rows = rows

    def __len__(self):
        return len(self._rows)

    @property
    def shape(self):
        return (len(self._rows), 1)

    class _ILoc:
        def __init__(self, rows):
            self._rows = rows

        def __getitem__(self, i):
            return self._rows[i]

    @property
    def iloc(self):
        return self._FakeDF_iloc

    def __init_subclass__(cls):  # pragma: no cover
        pass


def _master_acc_frame(acc_id):
    import pandas as pd
    return pd.DataFrame([{"acc_id": acc_id, "acc_role": "MASTER"}])


class _FakeCtx:
    def __init__(self, acc_id):
        self._acc_id = acc_id
        self.placed = False
        self.modified = False

    def get_acc_list(self):
        from common import RET_OK
        return RET_OK, _master_acc_frame(self._acc_id)

    def place_order(self, **kwargs):
        self.placed = True
        from common import RET_OK
        return RET_OK, "should-not-reach"

    def modify_order(self, **kwargs):
        self.modified = True
        from common import RET_OK
        return RET_OK, "should-not-reach"

    def close(self):
        pass


@pytest.fixture
def _trade_on_path():
    if TRADE_DIR not in sys.path:
        sys.path.insert(0, TRADE_DIR)
    scripts_dir = os.path.dirname(TRADE_DIR)
    if scripts_dir not in sys.path:
        sys.path.insert(0, scripts_dir)
    yield


def test_place_crypto_order_rejects_master(_trade_on_path, monkeypatch):
    pytest.importorskip("moomoo")  # needs SDK present to import the script
    import place_crypto_order as pco
    fake = _FakeCtx(acc_id=123)
    monkeypatch.setattr(pco, "create_crypto_trade_context", lambda **kw: fake)
    with pytest.raises(SystemExit) as exc:
        pco.place_crypto_order(code="CC.BTCUSD", side="BUY", quantity="0.01",
                               price=100.0, acc_id=123, confirmed=True)
    assert exc.value.code == 1
    assert fake.placed is False


def test_modify_order_rejects_master(_trade_on_path, monkeypatch):
    pytest.importorskip("moomoo")
    import modify_order as mo
    fake = _FakeCtx(acc_id=123)
    monkeypatch.setattr(mo, "create_trade_context", lambda *a, **kw: fake)
    with pytest.raises(SystemExit) as exc:
        mo.modify_order(order_id="1", price=10.0, quantity=1, acc_id=123)
    assert exc.value.code == 1
    assert fake.modified is False
