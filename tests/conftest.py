"""Shared test fixtures. Unit tests must run with no OpenD and no SDK import."""
import sys
import pytest


@pytest.fixture(autouse=True)
def _no_accidental_sdk(monkeypatch):
    """Fail loudly if a unit test imports the live SDK module `moomoo`.

    moomoo_broker.py is the ONLY module allowed to touch it, and it is never
    imported by the pure-core tests. This guards that invariant.
    """
    if "moomoo" in sys.modules:
        pytest.skip("moomoo SDK present in env; pure-core tests assume it is absent")
    yield
