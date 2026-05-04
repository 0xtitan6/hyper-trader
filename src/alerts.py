import logging
import queue
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
_QUEUE_MAX = 256


class Alerter(Protocol):
    def alert(self, level: str, message: str) -> None: ...

    def stop(self) -> None: ...


class WebhookAlerter:
    """Posts alerts to a Slack/Discord-compatible webhook.

    Local logging is always synchronous (preserves the forensic record even if
    the worker thread dies). Webhook delivery is dispatched to a daemon worker
    thread via a bounded queue so a slow webhook host can never block the WS
    handler thread that called `alert()`. If the queue is full, the alert is
    dropped with a warning rather than blocking.

    Pass `synchronous=True` to disable the worker thread (useful for tests).
    """

    def __init__(
        self,
        webhook_url: str,
        min_level: str = "warn",
        timeout_s: float = 3.0,
        post: object = None,
        synchronous: bool = False,
    ):
        if min_level not in _LEVEL_RANK:
            raise ValueError(f"min_level must be one of {list(_LEVEL_RANK)}, got {min_level!r}")
        self.webhook_url = webhook_url
        self.min_level = min_level
        self.timeout_s = timeout_s
        self._post = post or requests.post
        self.synchronous = synchronous

        self._queue: queue.Queue[tuple[str, str] | None] = queue.Queue(maxsize=_QUEUE_MAX)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        if not synchronous and webhook_url:
            self._thread = threading.Thread(target=self._run, daemon=True, name="alert-worker")
            self._thread.start()

    def alert(self, level: str, message: str) -> None:
        if level not in _LEVEL_RANK:
            log.warning("Unknown alert level %r; treating as 'info'", level)
            level = "info"
        log.log(_LEVEL_LOG[level], "[ALERT %s] %s", level.upper(), message)
        if not self.webhook_url:
            return
        if _LEVEL_RANK[level] < _LEVEL_RANK[self.min_level]:
            return
        if self.synchronous or self._thread is None:
            self._deliver(level, message)
            return
        try:
            self._queue.put_nowait((level, message))
        except queue.Full:
            log.warning("Alert queue full (%d) — dropping: [%s] %s", _QUEUE_MAX, level, message)

    def stop(self) -> None:
        """Signal the worker to drain and exit. Waits up to ~5s."""
        import contextlib

        if self._thread is None:
            return
        self._stop.set()
        with contextlib.suppress(queue.Full):
            self._queue.put_nowait(None)
        self._thread.join(timeout=5.0)
        self._thread = None

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                item = self._queue.get(timeout=0.5)
            except queue.Empty:
                continue
            if item is None:
                break
            level, message = item
            self._deliver(level, message)

    def _deliver(self, level: str, message: str) -> None:
        try:
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

    def stop(self) -> None:
        pass
