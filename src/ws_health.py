"""WS health monitor — detects silent WebSocket degradation and rebuilds.

The hyperliquid SDK's `WebsocketManager` has two latent bugs that combine to
cause silent feed loss:

1. `ws.run_forever()` is called without `reconnect=N`, so when the underlying
   websocket disconnects, the manager's run() returns and the thread exits
   silently. There is no auto-reconnect.

2. Even if reconnect were enabled, `on_open()` only replays
   `queued_subscriptions` — which is populated only when `ws_ready=False`.
   Subscriptions added after the initial connect (i.e. ALL of ours — they're
   issued post-start) go directly into `active_subscriptions` and would NOT
   be replayed on a reconnect.

Caught 2026-05-21: bot held mirrors for 5+ days while leaders had closed
on-chain; WS feed was silent but our own-fill sub also missed two
manually-submitted order fills 30+s after `exchange.order()` returned
`filled`. The PR-#30 LeaderReconciler now catches the consequence; this
module fixes the root cause.

**Design:**

- Wrap `info.subscribe()` so we track every (subscription, callback) pair.
- Wrap the callback to `touch()` a freshness timestamp on each message.
- Background thread polls every `check_interval_s`:
    - Is the SDK's `ws_manager` thread alive?
    - Is `ws_ready` still True?
    - Has any subscription received a message within `stale_threshold_s`?
- On detected death/staleness, stop the old manager, instantiate a new
  `WebsocketManager`, wait briefly for `ws_ready`, and replay every
  tracked subscription against the new manager.

This is defense-of-the-engine, not defense-in-depth. The LeaderReconciler
+ watchdog stay as safety nets in case the rebuild itself fails.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from typing import Any

from .alerts import Alerter, NullAlerter
from .protocols import InfoProto

log = logging.getLogger(__name__)


class WSHealthMonitor:
    """Wraps Info.subscribe to track + replay subscriptions on WS failure.

    Usage in main.py:

        info = Info(url)
        ws_health = WSHealthMonitor(info, alerter)
        # Route all subscribes through the monitor (preserves InfoProto contract)
        info.subscribe = ws_health.subscribe
        ws_health.start()
        # ... continue with normal Info usage. follower / positions still call
        # info.subscribe(); they're transparently wrapped.

    Stop with `ws_health.stop()` on shutdown.
    """

    def __init__(
        self,
        info: InfoProto,
        alerter: Alerter | None = None,
        stale_threshold_s: float = 300.0,
        check_interval_s: float = 30.0,
        rebuild_ready_wait_s: float = 4.0,
    ):
        self.info = info
        self.alerter: Alerter = alerter or NullAlerter()
        self.stale_threshold_s = stale_threshold_s
        self.check_interval_s = check_interval_s
        self.rebuild_ready_wait_s = rebuild_ready_wait_s
        # (subscription_dict, wrapped_callback) — wrapped includes touch()
        self._subs: list[tuple[dict[str, Any], Callable[[Any], None]]] = []
        self._last_msg_ts: float = time.time()
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._rebuild_count: int = 0

    def subscribe(self, subscription: dict[str, Any], callback: Callable[[Any], None]) -> int:
        """Drop-in replacement for `info.subscribe()`. Tracks the sub for replay.

        The callback is wrapped to update `_last_msg_ts` on each message —
        that's how we detect "WS still ready but server stopped sending."
        """
        def wrapped(msg: Any) -> None:
            with self._lock:
                self._last_msg_ts = time.time()
            try:
                callback(msg)
            except Exception:
                log.exception("WSHealthMonitor: user callback raised")

        # Subscribe via the SDK directly — we bypass our own wrapper to avoid
        # recursion (we may have monkey-patched info.subscribe to point at us).
        ws_mgr = getattr(self.info, "ws_manager", None)
        if ws_mgr is None:
            raise RuntimeError("Info has no ws_manager — was skip_ws=True?")
        sid = ws_mgr.subscribe(subscription, wrapped)

        with self._lock:
            self._subs.append((subscription, wrapped))
            self._last_msg_ts = time.time()  # reset baseline so a slow start doesn't trigger rebuild
        log.info("WSHealthMonitor: tracked subscription %s (total=%d)", subscription.get("type"), len(self._subs))
        return sid

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._run, daemon=True, name="ws-health")
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None

    @property
    def rebuild_count(self) -> int:
        with self._lock:
            return self._rebuild_count

    def last_msg_age_s(self) -> float:
        with self._lock:
            return time.time() - self._last_msg_ts

    def _run(self) -> None:
        while not self._stop.wait(self.check_interval_s):
            try:
                self._check_and_recover()
            except Exception:
                log.exception("WSHealthMonitor check raised — continuing")

    def _check_and_recover(self) -> None:
        ws_mgr = getattr(self.info, "ws_manager", None)
        thread_alive = bool(ws_mgr is not None and ws_mgr.is_alive())
        ws_ready = bool(ws_mgr is not None and getattr(ws_mgr, "ws_ready", False))
        age = self.last_msg_age_s()

        # Only treat staleness as an issue if we have subs at all — at startup
        # before any subs, age may exceed threshold without meaning anything.
        with self._lock:
            sub_count = len(self._subs)

        # Healthy: thread alive AND ready AND (no subs yet OR recent message)
        if thread_alive and ws_ready and (sub_count == 0 or age < self.stale_threshold_s):
            return

        log.warning(
            "WSHealthMonitor: unhealthy thread_alive=%s ws_ready=%s last_msg_age=%.0fs subs=%d — rebuilding",
            thread_alive, ws_ready, age, sub_count,
        )
        self.alerter.alert(
            "warn",
            f"WS rebuild: thread_alive={thread_alive} ws_ready={ws_ready} age={age:.0f}s subs={sub_count}",
        )
        self._rebuild()

    def _rebuild(self) -> None:
        """Stop old manager, instantiate new one, replay all tracked subs."""
        # Lazy import to avoid hard-coupling at module load
        from hyperliquid.websocket_manager import WebsocketManager

        old_mgr = getattr(self.info, "ws_manager", None)
        if old_mgr is not None:
            try:
                old_mgr.stop()
            except Exception:
                log.exception("WSHealthMonitor: error stopping old ws_manager (continuing)")

        new_mgr = WebsocketManager(self.info.base_url)
        new_mgr.start()

        # Wait for ws_ready briefly. If we replay before ready, subs go to
        # queued_subscriptions and replay correctly on the SDK's on_open path.
        # Either way it works, but waiting lets us know if the new connection
        # is genuinely opening.
        deadline = time.time() + self.rebuild_ready_wait_s
        while time.time() < deadline and not getattr(new_mgr, "ws_ready", False):
            time.sleep(0.1)

        with self._lock:
            subs_copy = list(self._subs)
        # Re-subscribe via the new manager. WebsocketManager.subscribe handles
        # both ready and not-yet-ready cases internally.
        replayed = 0
        for sub, wrapped in subs_copy:
            try:
                new_mgr.subscribe(sub, wrapped)
                replayed += 1
            except Exception:
                log.exception("WSHealthMonitor: failed to replay sub %s", sub.get("type"))

        self.info.ws_manager = new_mgr  # type: ignore[attr-defined]
        with self._lock:
            self._rebuild_count += 1
            # Give the new manager time to receive messages before we assess
            # staleness again — reset the clock.
            self._last_msg_ts = time.time()

        ready_after = getattr(new_mgr, "ws_ready", False)
        msg = (
            f"WS rebuild complete: replayed {replayed}/{len(subs_copy)} subs, "
            f"ws_ready={ready_after} (rebuild #{self._rebuild_count})"
        )
        log.warning(msg)
        self.alerter.alert("info" if ready_after else "warn", msg)
