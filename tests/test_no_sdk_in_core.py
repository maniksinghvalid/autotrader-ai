"""Enforce: importing the entire pure core never pulls in the `moomoo` SDK.

moomoo_broker.py is the ONLY module allowed to touch the SDK, and it does so
lazily (inside functions), so importing it — and even importing main.py, which
references MoomooBroker only inside main() — must NOT load `moomoo`. We check
this in a fresh subprocess so the result is independent of whatever other tests
have already imported in this session.
"""
import subprocess
import sys


def test_core_imports_do_not_load_moomoo_sdk():
    code = (
        "import importlib, sys\n"
        "for m in ('autotrader.domain', 'autotrader.config', 'autotrader.risk_core',\n"
        "          'autotrader.broker', 'autotrader.sim_broker', 'autotrader.router',\n"
        "          'autotrader.strategies.threshold', 'autotrader.main',\n"
        "          'autotrader.moomoo_broker'):\n"
        "    importlib.import_module(m)\n"
        "assert 'moomoo' not in sys.modules, 'core import pulled in the moomoo SDK'\n"
        "print('ok')\n"
    )
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert result.returncode == 0, (
        f"core import leaked the SDK or failed:\n"
        f"stdout={result.stdout}\nstderr={result.stderr}"
    )
    assert "ok" in result.stdout
