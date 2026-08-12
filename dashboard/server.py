#!/usr/bin/env python3
"""
AutoTrader operations dashboard — read-only Flask backend.

Design constraints (see CLAUDE.md):
  * READ-ONLY. This server only ever invokes the four read scripts in the
    allow-list below. It never places, modifies, cancels, or unlocks trades,
    and never calls unlock_trade / TrdUnlockTrade.
  * Default environment is paper (TrdEnv.SIMULATE). Live is never inferred.
  * OpenD must pass an is_opend_ready() health check (exponential-backoff
    polling) before any data endpoint will serve. If it never becomes ready,
    the endpoints return 503 instead of proceeding silently.
  * All Moomoo access is delegated to the vendored moomooapi scripts, which
    run their own common.py environment checks, set refresh_cache=True, and
    enforce the ret_code == RET_OK pattern. We do not reinvent that logic.
  * No hosts, ports, account ids, or intervals are hardcoded — they come from
    config/dashboard.config (non-secret) or environment variables.

Run:
    pip install flask
    python dashboard/server.py
Then open http://127.0.0.1:8787/ in a browser.
"""
import json
import logging
import os
import subprocess
import sys
import threading
import time

from flask import Flask, jsonify, request, send_from_directory

from opend_ready import is_opend_ready, OpenDNotReady, _resolve_endpoint

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s [dashboard] %(message)s")
logger = logging.getLogger("autotrader.dashboard.server")

# ------------------------------------------------------------------
# Paths
# ------------------------------------------------------------------
THIS_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.normpath(os.path.join(THIS_DIR, ".."))
TRADE_SCRIPTS = os.path.join(REPO_ROOT, "skills", "moomooapi", "scripts", "trade")
STATIC_DIR = os.path.join(THIS_DIR, "static")

# Read-only allow-list. Any script not named here is refused, so this server
# can never be repurposed to place or cancel orders.
ALLOWED_SCRIPTS = {
    "get_accounts": os.path.join(TRADE_SCRIPTS, "get_accounts.py"),
    "get_portfolio": os.path.join(TRADE_SCRIPTS, "get_portfolio.py"),
    "get_orders": os.path.join(TRADE_SCRIPTS, "get_orders.py"),
    "get_order_fill_list": os.path.join(TRADE_SCRIPTS, "get_order_fill_list.py"),
}

# Order statuses considered "open / working" (everything else is terminal).
OPEN_STATUSES = {
    "SUBMITTING", "SUBMITTED", "WAITING_SUBMIT", "FILLED_PART",
    "SUBMIT_FAILED", "TIMEOUT", "WAITING",
}


# ------------------------------------------------------------------
# Config (non-secret; secrets/connection come from FUTU_* env vars)
# ------------------------------------------------------------------
def load_config():
    """Load dashboard settings from config/dashboard.config (JSON) then env.

    Only non-secret presentation settings live here. OpenD connection details
    and credentials are read by the vendored common.get_config() from FUTU_*
    environment variables — never from this file.
    """
    defaults = {
        "web_host": "127.0.0.1",
        "web_port": 8787,
        "refresh_seconds": 30,        # client poll cadence; respects rate limits
        "opend_ready_timeout": 30,    # seconds to wait for OpenD on startup
        "preferred_market": "US",     # used to pick a market from trdmarket_auth
        "trd_env": "SIMULATE",        # paper by default; never auto-promoted
        "backtest_dir": "~/.autotrader/backtest_runs",   # python -m autotrader.backtest output root
    }
    cfg_path = os.path.join(REPO_ROOT, "config", "dashboard.config")
    if os.path.isfile(cfg_path):
        try:
            with open(cfg_path, "r", encoding="utf-8") as fh:
                file_cfg = json.load(fh)
            if isinstance(file_cfg, dict):
                defaults.update({k: file_cfg[k] for k in defaults if k in file_cfg})
            logger.info("Loaded dashboard config from %s", cfg_path)
        except (OSError, ValueError) as exc:
            logger.warning("Could not parse %s (%s); using defaults/env", cfg_path, exc)

    # Environment overrides (all optional).
    env_map = {
        "web_host": "DASHBOARD_HOST",
        "web_port": "DASHBOARD_PORT",
        "refresh_seconds": "DASHBOARD_REFRESH_SECONDS",
        "opend_ready_timeout": "DASHBOARD_OPEND_TIMEOUT",
        "preferred_market": "DASHBOARD_PREFERRED_MARKET",
        "backtest_dir": "DASHBOARD_BACKTEST_DIR",
    }
    for key, env in env_map.items():
        val = os.getenv(env)
        if val is not None and val != "":
            defaults[key] = val
    for int_key in ("web_port", "refresh_seconds", "opend_ready_timeout"):
        defaults[int_key] = int(defaults[int_key])

    # Hard safety: this dashboard is paper-only unless TRADING_ENV=LIVE is
    # explicitly set. We never infer live from anything else.
    if os.getenv("TRADING_ENV", "").strip().upper() == "LIVE":
        defaults["trd_env"] = "REAL"
    else:
        defaults["trd_env"] = "SIMULATE"
    return defaults


CONFIG = load_config()


# ------------------------------------------------------------------
# Script runner (delegates to vendored read-only scripts)
# ------------------------------------------------------------------
class ScriptError(RuntimeError):
    pass


def run_script(name, extra_args=None, timeout=25):
    """Invoke an allow-listed read-only script with --json and parse its output.

    Raises ScriptError on a non-zero exit, unparseable output, or an embedded
    {"error": ...} / {"ret": ...} payload — we never silently discard errors.
    """
    if name not in ALLOWED_SCRIPTS:
        raise ScriptError(f"Refused: '{name}' is not in the read-only allow-list")
    script_path = ALLOWED_SCRIPTS[name]
    if not os.path.isfile(script_path):
        raise ScriptError(f"Script not found: {script_path}")

    cmd = [sys.executable, script_path, "--json"]
    if extra_args:
        cmd.extend(extra_args)

    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout,
            cwd=REPO_ROOT,
        )
    except subprocess.TimeoutExpired as exc:
        raise ScriptError(f"{name} timed out after {timeout}s") from exc

    stdout = (proc.stdout or "").strip()
    if proc.returncode != 0 and not stdout:
        raise ScriptError(
            f"{name} exited {proc.returncode}: {(proc.stderr or '').strip()[:500]}"
        )

    # Scripts print a single JSON object on the last non-empty stdout line.
    payload = None
    for line in reversed(stdout.splitlines()):
        line = line.strip()
        if line.startswith("{"):
            try:
                payload = json.loads(line)
                break
            except ValueError:
                continue
    if payload is None:
        raise ScriptError(
            f"{name}: could not parse JSON output. stderr={(proc.stderr or '').strip()[:300]}"
        )

    if isinstance(payload, dict) and ("error" in payload or payload.get("ret", 0) not in (0, None)):
        detail = payload.get("error") or payload.get("hint") or payload
        raise ScriptError(f"{name} returned an error: {detail}")
    return payload


# ------------------------------------------------------------------
# OpenD readiness — checked once per session, cached
# ------------------------------------------------------------------
_session_state = {"opend_ready": False, "checked_at": 0.0, "error": None}
_state_lock = threading.Lock()


def ensure_opend_ready():
    """Confirm OpenD is ready (cached). Returns (ok, message)."""
    with _state_lock:
        if _session_state["opend_ready"]:
            return True, "ready"
    host, port = _resolve_endpoint()
    try:
        is_opend_ready(timeout=CONFIG["opend_ready_timeout"], host=host, port=port)
        with _state_lock:
            _session_state.update(opend_ready=True, checked_at=time.time(), error=None)
        return True, "ready"
    except OpenDNotReady as exc:
        with _state_lock:
            _session_state.update(opend_ready=False, checked_at=time.time(), error=str(exc))
        logger.error("OpenD readiness check failed: %s", exc)
        return False, str(exc)


# ------------------------------------------------------------------
# Account auto-detection
# ------------------------------------------------------------------
def detect_accounts():
    """Return simulated (paper) accounts unless TRADING_ENV=LIVE is set."""
    data = run_script("get_accounts")
    accounts = data.get("accounts", []) if isinstance(data, dict) else []
    want_env = CONFIG["trd_env"]
    out = []
    for a in accounts:
        if str(a.get("trd_env", "")).upper() != want_env:
            continue
        auth = a.get("trdmarket_auth") or []
        market = _pick_market(auth)
        out.append({
            "acc_id": a.get("acc_id"),
            "trd_env": a.get("trd_env"),
            "acc_type": a.get("acc_type"),
            "sim_acc_type": a.get("sim_acc_type"),
            "security_firm": a.get("security_firm"),
            "trdmarket_auth": auth,
            "market": market,
            "label": _account_label(a, market),
        })
    return out


def _pick_market(auth_list):
    """Choose a market for queries, preferring the configured one."""
    pref = str(CONFIG["preferred_market"]).upper()
    norm = [str(m).upper() for m in (auth_list or [])]
    if pref in norm:
        return pref
    for m in norm:
        if m in ("US", "HK", "CN", "SG", "HKCC"):
            return m
    return pref or "US"


def _account_label(a, market):
    sim = a.get("sim_acc_type") or ""
    typ = a.get("acc_type") or ""
    bits = [str(a.get("acc_id"))]
    if market:
        bits.append(market)
    if sim and sim != "NONE":
        bits.append(sim)
    elif typ:
        bits.append(typ)
    return " · ".join(bits)


# ------------------------------------------------------------------
# Snapshot assembly
# ------------------------------------------------------------------
def build_snapshot(acc_id, market):
    """Fetch portfolio + orders + fills for one account. Per-panel errors are
    captured rather than failing the whole snapshot."""
    common_args = ["--trd-env", CONFIG["trd_env"]]
    if acc_id:
        common_args += ["--acc-id", str(acc_id)]
    if market:
        common_args += ["--market", str(market)]

    snapshot = {
        "acc_id": acc_id, "market": market, "trd_env": CONFIG["trd_env"],
        "ts": time.time(), "errors": {},
    }

    # Portfolio (funds + positions)
    try:
        pf = run_script("get_portfolio", common_args)
        snapshot["funds"] = pf.get("funds", {}) or {}
        snapshot["positions"] = pf.get("positions", []) or []
    except ScriptError as exc:
        snapshot["funds"], snapshot["positions"] = {}, []
        snapshot["errors"]["portfolio"] = str(exc)

    # Orders -> split into open vs all-today
    try:
        od = run_script("get_orders", common_args)
        orders = od.get("orders", []) or []
        snapshot["orders_all"] = orders
        snapshot["orders_open"] = [
            o for o in orders
            if str(o.get("status", "")).upper() in OPEN_STATUSES
        ]
    except ScriptError as exc:
        snapshot["orders_all"], snapshot["orders_open"] = [], []
        snapshot["errors"]["orders"] = str(exc)

    # Today's fills
    try:
        fl = run_script("get_order_fill_list", common_args)
        snapshot["fills"] = fl.get("deals", []) or []
    except ScriptError as exc:
        snapshot["fills"] = []
        snapshot["errors"]["fills"] = str(exc)

    return snapshot


# ------------------------------------------------------------------
# Flask app
# ------------------------------------------------------------------
app = Flask(__name__, static_folder=None)


@app.route("/")
def index():
    return send_from_directory(STATIC_DIR, "index.html")


@app.route("/api/health")
def api_health():
    ok, msg = ensure_opend_ready()
    return jsonify({
        "opend_ready": ok,
        "message": msg,
        "trd_env": CONFIG["trd_env"],
        "refresh_seconds": CONFIG["refresh_seconds"],
    }), (200 if ok else 503)


@app.route("/api/accounts")
def api_accounts():
    ok, msg = ensure_opend_ready()
    if not ok:
        return jsonify({"error": "OpenD not ready", "detail": msg}), 503
    try:
        return jsonify({"accounts": detect_accounts(), "trd_env": CONFIG["trd_env"]})
    except ScriptError as exc:
        logger.error("account detection failed: %s", exc)
        return jsonify({"error": str(exc)}), 502


@app.route("/api/snapshot")
def api_snapshot():
    ok, msg = ensure_opend_ready()
    if not ok:
        return jsonify({"error": "OpenD not ready", "detail": msg}), 503

    acc_id = request.args.get("acc_id", type=int)
    market = request.args.get("market", default=None)

    # If no account specified, auto-detect and use the first paper account.
    if not acc_id:
        try:
            accts = detect_accounts()
        except ScriptError as exc:
            return jsonify({"error": str(exc)}), 502
        if not accts:
            return jsonify({"error": "No paper (SIMULATE) accounts found"}), 404
        acc_id = accts[0]["acc_id"]
        market = market or accts[0]["market"]

    return jsonify(build_snapshot(acc_id, market))


# ------------------------------------------------------------------
# Backtests — read-only viewer over saved `python -m autotrader.backtest`
# runs. Pure local file reads: no OpenD gate, no broker access, no
# subprocess. Serves only the fixed artifact filenames a backtest run
# writes; anything else, or any run_id that would escape the runs root,
# is refused.
# ------------------------------------------------------------------
BACKTEST_ARTIFACT_NAMES = {"report.html", "summary.json", "trades.csv", "equity_curve.csv"}


def _backtest_runs_dir():
    return os.path.abspath(os.path.expanduser(str(CONFIG["backtest_dir"])))


@app.route("/api/backtests")
def api_backtests():
    root = _backtest_runs_dir()
    runs = []
    if os.path.isdir(root):
        for name in os.listdir(root):
            run_dir = os.path.join(root, name)
            summary_path = os.path.join(run_dir, "summary.json")
            if not os.path.isdir(run_dir) or not os.path.isfile(summary_path):
                continue
            try:
                with open(summary_path, "r", encoding="utf-8") as fh:
                    summary = json.load(fh)
            except (OSError, ValueError) as exc:
                logger.warning("backtests: could not read %s: %s", summary_path, exc)
                continue
            portfolio = summary.get("portfolio", {}) if isinstance(summary, dict) else {}
            runs.append({
                "run_id": name,
                "mtime": os.path.getmtime(run_dir),
                "total_return": portfolio.get("total_return"),
                "sharpe": portfolio.get("sharpe"),
                "max_drawdown": portfolio.get("max_drawdown"),
                "trade_count": portfolio.get("trade_count"),
                "final_value": portfolio.get("final_value"),
            })
    runs.sort(key=lambda r: r["mtime"], reverse=True)
    return jsonify({"runs": runs})


@app.route("/backtests/<run_id>/<filename>")
def api_backtest_artifact(run_id, filename):
    if filename not in BACKTEST_ARTIFACT_NAMES or run_id in ("", ".", ".."):
        return jsonify({"error": "not found"}), 404
    root = os.path.realpath(_backtest_runs_dir())
    run_dir = os.path.realpath(os.path.join(root, run_id))
    if run_dir != root and not run_dir.startswith(root + os.sep):
        return jsonify({"error": "not found"}), 404
    if not os.path.isfile(os.path.join(run_dir, filename)):
        return jsonify({"error": "not found"}), 404
    return send_from_directory(run_dir, filename)


def main():
    host, port = CONFIG["web_host"], CONFIG["web_port"]
    logger.info("AutoTrader dashboard — environment: %s (paper unless TRADING_ENV=LIVE)",
                CONFIG["trd_env"])
    if CONFIG["trd_env"] == "REAL":
        logger.warning("TRADING_ENV=LIVE detected — dashboard is showing the LIVE account (read-only).")
    # Pre-flight: confirm OpenD before announcing the server is up.
    ok, msg = ensure_opend_ready()
    if ok:
        logger.info("OpenD health check passed.")
    else:
        logger.warning("OpenD not ready at startup: %s", msg)
        logger.warning("Server will start, but data endpoints return 503 until OpenD is up.")
    logger.info("Serving dashboard at http://%s:%s/", host, port)
    app.run(host=host, port=port, debug=False, use_reloader=False)


if __name__ == "__main__":
    main()
