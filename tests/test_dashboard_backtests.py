"""Dashboard Backtests tab: read-only file-serving endpoints over saved
`python -m autotrader.backtest` runs. No OpenD gate, no broker access, no
subprocess — pure local file reads scoped to a runs directory."""
from __future__ import annotations

import json
import os
import sys

import pytest

pytest.importorskip("flask")

_DASHBOARD_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "dashboard")
if _DASHBOARD_DIR not in sys.path:
    sys.path.insert(0, _DASHBOARD_DIR)

import server  # noqa: E402  (dashboard/server.py; path inserted above)


def _client(tmp_path):
    server.CONFIG["backtest_dir"] = str(tmp_path)
    server.app.config.update(TESTING=True)
    return server.app.test_client()


def _seed_run(tmp_path, run_id, portfolio):
    run_dir = tmp_path / run_id
    run_dir.mkdir(parents=True)
    (run_dir / "summary.json").write_text(
        json.dumps({"portfolio": portfolio, "per_symbol": {}}), encoding="utf-8")
    (run_dir / "report.html").write_text(f"<html>{run_id}</html>", encoding="utf-8")
    (run_dir / "trades.csv").write_text("ts,symbol\n1,AAPL\n", encoding="utf-8")
    (run_dir / "equity_curve.csv").write_text("date,total_value,cash\n2026-01-01,100,100\n", encoding="utf-8")
    return run_dir


def test_api_backtests_lists_runs_newest_first_no_opend_needed(tmp_path):
    _seed_run(tmp_path, "run-old", {"total_return": 0.1})
    os.utime(tmp_path / "run-old", (1_000_000, 1_000_000))
    _seed_run(tmp_path, "run-new", {"total_return": 0.2})
    os.utime(tmp_path / "run-new", (2_000_000, 2_000_000))
    client = _client(tmp_path)
    resp = client.get("/api/backtests")
    assert resp.status_code == 200
    runs = resp.get_json()["runs"]
    assert [r["run_id"] for r in runs] == ["run-new", "run-old"]
    assert runs[0]["total_return"] == pytest.approx(0.2)


def test_api_backtests_empty_dir_returns_empty_list_not_error(tmp_path):
    client = _client(tmp_path)
    resp = client.get("/api/backtests")
    assert resp.status_code == 200
    assert resp.get_json()["runs"] == []


def test_api_backtests_missing_dir_returns_empty_list_not_error(tmp_path):
    client = _client(tmp_path / "does-not-exist")
    resp = client.get("/api/backtests")
    assert resp.status_code == 200
    assert resp.get_json()["runs"] == []


def test_api_backtests_ignores_run_without_summary_json(tmp_path):
    (tmp_path / "half-written").mkdir()
    (tmp_path / "half-written" / "report.html").write_text("<html></html>")
    client = _client(tmp_path)
    resp = client.get("/api/backtests")
    assert resp.get_json()["runs"] == []


def test_backtest_artifact_serves_allowed_file(tmp_path):
    _seed_run(tmp_path, "run1", {"total_return": 0.05})
    client = _client(tmp_path)
    resp = client.get("/backtests/run1/report.html")
    assert resp.status_code == 200
    assert b"run1" in resp.data


def test_backtest_artifact_rejects_non_whitelisted_filename(tmp_path):
    run_dir = _seed_run(tmp_path, "run1", {"total_return": 0.05})
    (run_dir / "secret.txt").write_text("nope")
    client = _client(tmp_path)
    resp = client.get("/backtests/run1/secret.txt")
    assert resp.status_code == 404


def test_backtest_artifact_rejects_dotdot_run_id(tmp_path):
    _seed_run(tmp_path, "run1", {"total_return": 0.05})
    (tmp_path.parent / "outside_marker.txt").write_text("must never be reachable")
    client = _client(tmp_path)
    resp = client.get("/backtests/../outside_marker.txt")
    assert resp.status_code == 404


def test_backtest_artifact_missing_run_404(tmp_path):
    client = _client(tmp_path)
    resp = client.get("/backtests/does-not-exist/report.html")
    assert resp.status_code == 404
