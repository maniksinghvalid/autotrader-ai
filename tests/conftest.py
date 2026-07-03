"""Shared test fixtures.

The invariant "pure-core modules never import the live `moomoo` SDK" is enforced
by tests/test_no_sdk_in_core.py (a subprocess import check), NOT by a session
autouse fixture. The earlier autouse guard skipped on `moomoo in sys.modules`,
which became order-dependent and wrong once the SDK is actually installed: the
first test to load the SDK (R19 / live) would then cause every later test in the
same session to skip. A subprocess check verifies the real property without that
cross-test contamination.
"""
