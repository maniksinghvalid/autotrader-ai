"""Offline backtesting engine for the production breakout strategy.

No Moomoo/SDK imports anywhere in this package — see tests/test_no_sdk_in_core.py.
Historical data comes from the Massive API (formerly Polygon.io); the engine
drives the SAME strategy/exit/sizing functions main.py uses live so results
reflect real AutoTrader behavior."""
from __future__ import annotations
