import logging
import threading
from typing import Protocol

import requests

log = logging.getLogger(__name__)

_LEVEL_RANK = {"info": 0, "warn": 1, "error": 2, "critical": 3}
_LEVEL_LOG = {
    "info": logging.INFO,
    "warn": logging.WARNING,
    "error": logging.ERROR,
    "critical": logging.CRITICAL,
}


class Alerter(Protocol):
    def alert(self, level: str, message: str) -> None: ...


class WebhookAlerter:
    """Posts alerts to a Slack/Discord-compatible webhook. Best-effort: webhook
    failures are logged but never raise. Always logs the alert locally regardless.
    """

    def __init__(
        self,
        webhook_url: str,
        min_level: str = "warn",
        timeout_s: float = 3.0,
        post: object = None,
    ):
        if min_level not in _LEVEL_RANK:
            raise ValueError(f"min_level must be one of {list(_LEVEL_RANK)}, got {min_level!r}")
        self.webhook_url = webhook_url
        self.min_level = min_level
        self.timeout_s = timeout_s
        self._post = post or requests.post
        self._lock = threading.Lock()

    def alert(self, level: str, message: str) -> None:
        if level not in _LEVEL_RANK:
            log.warning("Unknown alert level %r; treating as 'info'", level)
            level = "info"
        log.log(_LEVEL_LOG[level], "[ALERT %s] %s", level.upper(), message)
        if not self.webhook_url:
            return
        if _LEVEL_RANK[level] < _LEVEL_RANK[self.min_level]:
            return
        try:
            with self._lock:
                self._post(  # type: ignore[operator]
                    self.webhook_url,
                    json={"text": f"[{level.upper()}] {message}"},
                    timeout=self.timeout_s,
                )
        except Exception:
            log.warning("Failed to deliver alert to webhook", exc_info=True)


class NullAlerter:
    """No-op alerter; useful in tests and when webhook is disabled."""

    def alert(self, level: str, message: str) -> None:
        log.info("[ALERT %s] %s", level.upper(), message)
