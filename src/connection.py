import logging
import threading
import time
from collections.abc import Callable

from .alerts import Alerter

log = logging.getLogger(__name__)


class ConnectionHealth:
    """Tracks the timestamp of the last received WS message and alerts when stale.

    Call `touch()` from each WS handler. Background thread checks every
    `check_interval_s` and fires an `error` alert when the threshold is exceeded;
    fires an `info` recovery alert when traffic resumes.

    Optionally invokes `on_stale` when the stale alert fires — used to trigger
    REST-fetch backfill of missed fills.
    """

    def __init__(
        self,
        alerter: Alerter,
        stale_threshold_s: float = 120.0,
        check_interval_s: float = 15.0,
        on_stale: Callable[[], None] | None = None,
    ):
        if stale_threshold_s <= 0:
            raise ValueError("stale_threshold_s must be > 0")
        self.alerter = alerter
        self.stale_threshold_s = stale_threshold_s
        self.check_interval_s = check_interval_s
        self.on_stale = on_stale
        self._last_msg_ts = time.time()
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._was_stale = False

    def touch(self) -> None:
        with self._lock:
            self._last_msg_ts = time.time()
            was_stale = self._was_stale
            self._was_stale = False
        if was_stale:
            self.alerter.alert("info", "WS connection recovered")

    def last_msg_age_s(self) -> float:
        with self._lock:
            return time.time() - self._last_msg_ts

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._run, daemon=True, name="conn-health")
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None

    def _run(self) -> None:
        while not self._stop.wait(self.check_interval_s):
            age = self.last_msg_age_s()
            if age <= self.stale_threshold_s:
                continue
            with self._lock:
                fire = not self._was_stale
                self._was_stale = True
            if not fire:
                continue
            self.alerter.alert(
                "error",
                f"WS stale: no messages in {age:.0f}s (threshold {self.stale_threshold_s:.0f}s)",
            )
            if self.on_stale is not None:
                try:
                    self.on_stale()
                except Exception:
                    log.exception("on_stale callback raised")
