"""Best-effort Slack alerting for operational events (HALT, watchdog death,
job failures). NEVER raises into the trading loop; `key` gives once-per-episode
dedup so a repeating condition (e.g. HALTED_UNHEALTHY every 5s iteration)
alerts exactly once until reset. No SDK import."""
from __future__ import annotations

import logging
from typing import Callable, Optional, Set

logger = logging.getLogger("autotrader.alerts")


def _default_post(url: str, payload: dict) -> int:
    from autotrader.reporting.eod_reporter import post_slack  # lazy: keep import cheap
    return post_slack(url, payload)


class AlertSink:
    def __init__(self, url: Optional[str], post: Optional[Callable] = None):
        self._url = url
        self._post = post or _default_post
        self._fired: Set[str] = set()

    def send(self, text: str, key: Optional[str] = None) -> bool:
        if key is not None and key in self._fired:
            return False
        if self._url is None:
            logger.warning("ALERT (no Slack URL configured): %s", text)
            return False
        try:
            status = self._post(self._url, {"text": text})
        except Exception as e:            # alerting must never break the loop
            logger.error("alert POST failed: %s (text=%r)", e, text)
            return False
        if not (200 <= status < 300):
            logger.error("alert POST non-2xx: %s (text=%r)", status, text)
            return False
        if key is not None:
            self._fired.add(key)
        return True

    def reset(self, key: str) -> None:
        self._fired.discard(key)
