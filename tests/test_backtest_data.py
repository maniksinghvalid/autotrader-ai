"""Massive API client + cache: offline, zero network, zero real sleep."""
from __future__ import annotations

import json
import urllib.error

import pytest

from autotrader.backtest.data import (
    Bar, MassiveClient, MassiveError, _clean_bars, load_bars, normalize_ticker,
)


class _FakeResp:
    def __init__(self, body: bytes):
        self._body = body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self):
        return self._body


def _page(bars):
    return json.dumps({"results": bars}).encode()


def test_pagination_merges_pages_and_reattaches_auth_header():
    """next_url carries no auth of its own — each follow-up request must
    still send the Bearer header."""
    page1 = {"results": [{"t": 1000, "o": 1, "h": 2, "l": 0.5, "c": 1.5, "v": 100}],
             "next_url": "https://api.massive.com/v2/aggs/next-page"}
    page2 = {"results": [{"t": 2000, "o": 1.5, "h": 2.5, "l": 1, "c": 2, "v": 200}]}
    responses = [json.dumps(page1).encode(), json.dumps(page2).encode()]
    calls = []

    def opener(req):
        calls.append(req)
        return _FakeResp(responses.pop(0))

    client = MassiveClient("KEY", opener=opener, sleep=lambda s: None)
    bars = client.aggs("AAPL", 1, "day", "2026-01-01", "2026-01-05")
    assert [b.ts for b in bars] == [1000, 2000]
    assert len(calls) == 2
    for req in calls:
        assert req.get_header("Authorization") == "Bearer KEY"


def test_429_then_200_retries_with_injected_sleep_no_real_delay():
    sleeps = []
    attempts = {"n": 0}

    def opener(req):
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise urllib.error.HTTPError(req.full_url, 429, "rate limited", {}, None)
        return _FakeResp(_page([{"t": 1000, "o": 1, "h": 2, "l": 0.5, "c": 1.5, "v": 1}]))

    client = MassiveClient("KEY", opener=opener, sleep=sleeps.append)
    bars = client.aggs("AAPL", 1, "day", "2026-01-01", "2026-01-05")
    assert len(bars) == 1
    assert sleeps == [15.0]


def test_429_exhausts_retries_raises_massive_error():
    def opener(req):
        raise urllib.error.HTTPError(req.full_url, 429, "rate limited", {}, None)

    client = MassiveClient("KEY", opener=opener, sleep=lambda s: None)
    with pytest.raises(MassiveError):
        client.aggs("AAPL", 1, "day", "2026-01-01", "2026-01-05")


def test_non_retryable_http_error_raises_immediately():
    calls = {"n": 0}

    def opener(req):
        calls["n"] += 1
        raise urllib.error.HTTPError(req.full_url, 403, "forbidden", {}, None)

    client = MassiveClient("KEY", opener=opener, sleep=lambda s: None)
    with pytest.raises(MassiveError):
        client.aggs("AAPL", 1, "day", "2026-01-01", "2026-01-05")
    assert calls["n"] == 1


def test_load_bars_cache_hit_makes_zero_network_calls(tmp_path):
    calls = []

    def opener(req):
        calls.append(req)
        return _FakeResp(_page([{"t": 1000, "o": 1, "h": 2, "l": 0.5, "c": 1.5, "v": 1}]))

    factory = lambda: MassiveClient("KEY", opener=opener, sleep=lambda s: None)
    bars1 = load_bars(factory, tmp_path, "AAPL", 1, "day", "2026-01-01", "2026-01-05")
    assert len(calls) == 1
    bars2 = load_bars(factory, tmp_path, "AAPL", 1, "day", "2026-01-01", "2026-01-05")
    assert len(calls) == 1
    assert bars1 == bars2


def test_load_bars_no_cache_flag_always_hits_network(tmp_path):
    calls = []

    def opener(req):
        calls.append(req)
        return _FakeResp(_page([{"t": 1000, "o": 1, "h": 2, "l": 0.5, "c": 1.5, "v": 1}]))

    factory = lambda: MassiveClient("KEY", opener=opener, sleep=lambda s: None)
    load_bars(factory, tmp_path, "AAPL", 1, "day", "2026-01-01", "2026-01-05", use_cache=False)
    load_bars(factory, tmp_path, "AAPL", 1, "day", "2026-01-01", "2026-01-05", use_cache=False)
    assert len(calls) == 2


def test_load_bars_cache_hit_never_calls_factory_needs_no_key(tmp_path):
    """A cache hit must not even construct a client — a fully cached run
    needs no API key at all."""
    calls = []

    def opener(req):
        calls.append(req)
        return _FakeResp(_page([{"t": 1000, "o": 1, "h": 2, "l": 0.5, "c": 1.5, "v": 1}]))

    def factory():
        raise AssertionError("client_factory must not be called on a cache hit")

    priming = lambda: MassiveClient("KEY", opener=opener, sleep=lambda s: None)
    load_bars(priming, tmp_path, "AAPL", 1, "day", "2026-01-01", "2026-01-05")
    bars = load_bars(factory, tmp_path, "AAPL", 1, "day", "2026-01-01", "2026-01-05")
    assert len(bars) == 1


def test_clean_bars_dedupes_sorts_and_drops_invalid():
    raw = [
        {"t": 2000, "o": 1, "h": 2, "l": 1, "c": 1.5, "v": 1},
        {"t": 1000, "o": 1, "h": 2, "l": 1, "c": 1.5, "v": 1},
        {"t": 1000, "o": 9, "h": 9, "l": 9, "c": 9, "v": 9},   # dup ts, first wins
        {"t": 3000, "o": 0, "h": 2, "l": 1, "c": 1.5, "v": 1},  # invalid: o<=0
        {"t": 4000, "o": 1, "h": 0.5, "l": 1, "c": 1.5, "v": 1},  # high < low
    ]
    bars = _clean_bars(raw)
    assert [b.ts for b in bars] == [1000, 2000]
    assert bars[0].open == 1


def test_bar_rejects_high_below_low():
    with pytest.raises(ValueError):
        Bar(ts=1, open=1, high=0.5, low=1, close=1, volume=1)


@pytest.mark.parametrize("raw,expected", [
    ("US.AAPL", "AAPL"),
    ("aapl", "AAPL"),
    ("AAPL", "AAPL"),
])
def test_normalize_ticker(raw, expected):
    assert normalize_ticker(raw) == expected


def test_missing_api_key_raises_naming_secure_config():
    with pytest.raises(MassiveError, match="secure.config"):
        MassiveClient("")
