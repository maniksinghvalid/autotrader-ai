#!/usr/bin/env python3
"""
OpenD readiness gate for the AutoTrader operations dashboard.

Per project rules, no fixed time.sleep() may be used as a readiness check.
This module polls the OpenD gateway with an exponential-backoff loop until it
accepts a TCP connection, then raises a hard exception if it never becomes
ready within the configured timeout.

Host/port are read from config/env via the vendored moomooapi common.get_config()
helper (FUTU_OPEND_HOST / FUTU_OPEND_PORT). Nothing is hardcoded here.
"""
import os
import socket
import sys
import time
import logging

logger = logging.getLogger("autotrader.dashboard.opend")

# Resolve the moomooapi scripts dir so we can reuse common.get_config().
_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.normpath(os.path.join(_THIS_DIR, ".."))
_MOOMOO_SCRIPTS = os.path.join(_REPO_ROOT, "skills", "moomooapi", "scripts")


def _resolve_endpoint():
    """Return (host, port) from config/env, never hardcoded in this module.

    Prefers the vendored common.get_config(); falls back to the documented
    FUTU_* environment variables if the SDK module cannot be imported (e.g.
    during offline unit tests).
    """
    if _MOOMOO_SCRIPTS not in sys.path:
        sys.path.insert(0, _MOOMOO_SCRIPTS)
    try:
        from common import get_config  # type: ignore
        cfg = get_config()
        return cfg.opend_host, int(cfg.opend_port)
    except Exception as exc:  # SDK not importable offline — fall back to env.
        logger.debug("common.get_config() unavailable (%s); using env vars", exc)
        host = os.getenv("FUTU_OPEND_HOST", "127.0.0.1")
        port = int(os.getenv("FUTU_OPEND_PORT", "11111"))
        return host, port


class OpenDNotReady(RuntimeError):
    """Raised when OpenD never accepts connections within the timeout."""


def _can_connect(host, port, connect_timeout):
    """One non-blocking probe. Returns True if a TCP connection succeeds."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(connect_timeout)
    try:
        sock.connect((host, port))
        return True
    except (ConnectionRefusedError, OSError):
        return False
    finally:
        sock.close()


def is_opend_ready(timeout=30.0, initial_backoff=0.5, max_backoff=5.0,
                   connect_timeout=2.0, host=None, port=None):
    """Poll OpenD until it accepts connections, using exponential backoff.

    This is the first call the dashboard makes every session. It actively polls
    rather than sleeping a fixed amount, and the delay between probes grows
    exponentially (capped at max_backoff) until the overall timeout elapses.

    Returns True once OpenD is reachable. Raises OpenDNotReady on timeout so the
    caller can halt instead of proceeding silently.
    """
    if host is None or port is None:
        host, port = _resolve_endpoint()

    deadline = time.monotonic() + timeout
    backoff = initial_backoff
    attempt = 0

    while True:
        attempt += 1
        if _can_connect(host, port, connect_timeout):
            logger.info("OpenD ready at %s:%s (attempt %d)", host, port, attempt)
            return True

        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise OpenDNotReady(
                f"OpenD did not become ready at {host}:{port} within {timeout:.0f}s "
                f"after {attempt} attempts. Start the OpenD GUI client and confirm "
                f"it is authenticated, then retry."
            )

        # Sleep is a backoff between active probes, not a fixed readiness wait.
        delay = min(backoff, max_backoff, max(remaining, 0.0))
        logger.debug("OpenD not ready (attempt %d); retrying in %.1fs", attempt, delay)
        time.sleep(delay)
        backoff = min(backoff * 2, max_backoff)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    h, p = _resolve_endpoint()
    try:
        is_opend_ready(host=h, port=p)
        print(f"OpenD is ready at {h}:{p}")
    except OpenDNotReady as e:
        print(f"OpenD NOT ready: {e}", file=sys.stderr)
        sys.exit(1)
