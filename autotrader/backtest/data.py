"""Massive API (formerly Polygon.io) historical bar retrieval + local cache.

Stdlib only (urllib) — CLAUDE.md: no new deps for a few lines of HTTP, and no
Moomoo/SDK import (see tests/test_no_sdk_in_core.py). `opener`/`sleep` are
injected so tests run with zero network and zero real delay."""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Callable, List
from zoneinfo import ZoneInfo

_ET = ZoneInfo("America/New_York")
_DEFAULT_BASE_URL = "https://api.massive.com"
_MAX_RETRIES = 5
_RETRY_SLEEP_SECONDS = 15.0

DEFAULT_CACHE_DIR = Path.home() / ".autotrader" / "backtest_cache"


class MassiveError(RuntimeError):
    """Raised on missing/invalid API key, exhausted retries, or a malformed response."""


@dataclass(frozen=True)
class Bar:
    """One OHLCV bar. `ts` is the Massive bar-start unix-ms timestamp."""
    ts: int
    open: float
    high: float
    low: float
    close: float
    volume: float

    def __post_init__(self):
        if self.high < self.low:
            raise ValueError(f"Bar high {self.high} < low {self.low}")

    def day(self) -> date:
        """Session date in US Eastern time (bars are ET-aligned per Massive)."""
        return datetime.fromtimestamp(self.ts / 1000, tz=timezone.utc).astimezone(_ET).date()


def normalize_ticker(symbol: str) -> str:
    """"US.AAPL" -> "AAPL" (Massive tickers are bare); case-insensitive."""
    s = symbol.strip().upper()
    if s.startswith("US."):
        s = s[3:]
    return s


def _clean_bars(raw: List[dict]) -> List[Bar]:
    """Sort by ts, drop duplicate ts (keep first), drop invalid bars. No
    gap-filling — a missing session is simply not evaluated, matching
    production's fail-safe (moomoo_broker.recent_high returns None rather
    than fabricate data)."""
    bars = []
    for r in raw:
        o, h, l, c = r.get("o"), r.get("h"), r.get("l"), r.get("c")
        if any(x is None or x <= 0 for x in (o, h, l, c)) or h < l:
            continue
        bars.append(Bar(ts=int(r["t"]), open=float(o), high=float(h),
                        low=float(l), close=float(c), volume=float(r.get("v", 0))))
    bars.sort(key=lambda b: b.ts)
    seen = set()
    deduped = []
    for b in bars:
        if b.ts in seen:
            continue
        seen.add(b.ts)
        deduped.append(b)
    return deduped


class MassiveClient:
    """Thin REST client for Massive's aggregate-bars endpoint."""

    def __init__(self, api_key: str, base_url: str = _DEFAULT_BASE_URL,
                 opener: Callable[[urllib.request.Request], object] = urllib.request.urlopen,
                 sleep: Callable[[float], None] = time.sleep):
        if not api_key:
            raise MassiveError(
                "MASSIVE_API_KEY is not set — add it to config/secure.config "
                "(see config/secure.config.example)")
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._opener = opener
        self._sleep = sleep

    def aggs(self, ticker: str, multiplier: int, timespan: str, frm: str, to: str,
             adjusted: bool = True) -> List[Bar]:
        """GET /v2/aggs/ticker/{ticker}/range/{multiplier}/{timespan}/{frm}/{to},
        following next_url pagination (re-attaching the Bearer header each
        request — next_url itself carries no auth) and retrying 429/5xx with
        a bounded backoff."""
        url = (f"{self._base_url}/v2/aggs/ticker/{ticker}/range/{multiplier}/"
               f"{timespan}/{frm}/{to}?adjusted={'true' if adjusted else 'false'}"
               f"&sort=asc&limit=50000")
        raw: List[dict] = []
        attempts = 0
        while url is not None:
            req = urllib.request.Request(
                url, headers={"Authorization": f"Bearer {self._api_key}"})
            try:
                with self._opener(req) as resp:
                    body = resp.read()
            except urllib.error.HTTPError as exc:
                if exc.code == 429 or exc.code >= 500:
                    attempts += 1
                    if attempts > _MAX_RETRIES:
                        raise MassiveError(
                            f"Massive API: exhausted retries fetching {ticker} "
                            f"({exc.code})") from exc
                    self._sleep(_RETRY_SLEEP_SECONDS)
                    continue
                raise MassiveError(f"Massive API error {exc.code} fetching {ticker}: "
                                   f"{exc.reason}") from exc
            attempts = 0
            try:
                payload = json.loads(body)
            except ValueError as exc:
                raise MassiveError(f"Massive API: malformed JSON for {ticker}") from exc
            raw.extend(payload.get("results") or [])
            url = payload.get("next_url")
        return _clean_bars(raw)


def _cache_path(cache_dir: Path, ticker: str, multiplier: int, timespan: str,
                frm: str, to: str, adjusted: bool) -> Path:
    adj = "1" if adjusted else "0"
    return cache_dir / f"{ticker}_{multiplier}{timespan}_{frm}_{to}_adj{adj}.json"


def load_bars(client_factory: Callable[[], "MassiveClient"], cache_dir: Path, ticker: str,
             multiplier: int, timespan: str, frm: str, to: str, adjusted: bool = True,
             use_cache: bool = True) -> List[Bar]:
    """Fetch bars for one exact request, via a local JSON cache keyed on the
    full request. `client_factory` is called ONLY on a cache miss — a cache
    hit makes zero network calls and needs no API key at all (the factory,
    not a concrete client, is what makes this deferral possible: building a
    MassiveClient eagerly would demand a key even when every symbol is
    already cached). # ponytail: whole-range cache key; re-shard per month
    if API quota ever becomes the bottleneck."""
    cache_dir = Path(cache_dir)
    path = _cache_path(cache_dir, ticker, multiplier, timespan, frm, to, adjusted)
    if use_cache and path.is_file():
        with path.open("r", encoding="utf-8") as fh:
            cached = json.load(fh)
        return [Bar(**b) for b in cached["bars"]]

    client = client_factory()
    bars = client.aggs(ticker, multiplier, timespan, frm, to, adjusted=adjusted)

    if use_cache:
        cache_dir.mkdir(parents=True, exist_ok=True)
        payload = {"bars": [
            {"ts": b.ts, "open": b.open, "high": b.high, "low": b.low,
             "close": b.close, "volume": b.volume} for b in bars
        ]}
        tmp = path.with_suffix(".tmp")
        with tmp.open("w", encoding="utf-8") as fh:
            json.dump(payload, fh)
        tmp.replace(path)
    return bars
