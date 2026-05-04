import logging
import threading
import time
from collections.abc import Callable
from typing import Any

from .connection import ConnectionHealth
from .protocols import InfoProto
from .state import State

log = logging.getLogger(__name__)

FillCallback = Callable[[str, dict], None]


class FillFollower:
    """Subscribes to leader userFills WS, dedupes by tid via persistent state,
    forwards new fills to a callback. Provides REST `backfill()` to recover
    fills missed during a WS gap.
    """

    def __init__(
        self,
        info: InfoProto,
        on_fill: FillCallback,
        state: State,
        health: ConnectionHealth | None = None,
    ):
        self.info = info
        self.on_fill = on_fill
        self.state = state
        self.health = health
        self._subscribed: set[str] = set()
        self._lock = threading.Lock()

    @property
    def addresses(self) -> list[str]:
        with self._lock:
            return list(self._subscribed)

    def follow(self, addresses: list[str]) -> None:
        for raw in addresses:
            addr = raw.lower()
            with self._lock:
                if addr in self._subscribed:
                    continue
                self._subscribed.add(addr)
            log.info("Subscribing to fills for leader %s", addr)
            self.info.subscribe(
                {"type": "userFills", "user": addr},
                lambda msg, a=addr: self._handle(a, msg),
            )

    def backfill(self, since_ms: int | None = None) -> int:
        """REST-fetch userFillsByTime for each subscribed leader and dispatch any
        new (un-deduped) fills. Defaults to last 10 minutes if since_ms is None.

        Returns number of new fills dispatched.
        """
        if since_ms is None:
            since_ms = int((time.time() - 600) * 1000)
        new_count = 0
        for addr in self.addresses:
            try:
                fills = (
                    self.info.post(
                        "/info",
                        {"type": "userFillsByTime", "user": addr, "startTime": since_ms},
                    )
                    or []
                )
            except Exception:
                log.exception("Backfill REST fetch failed for %s", addr[:10])
                continue
            if not isinstance(fills, list):
                continue
            for f in fills:
                if not isinstance(f, dict):
                    continue
                tid = f.get("tid")
                if tid is None:
                    continue
                if not self.state.mark_tid_seen(int(tid), addr):
                    continue
                try:
                    self.on_fill(addr, f)
                    new_count += 1
                except Exception:
                    log.exception("Backfill handler error for %s", addr[:10])
        log.info("Backfill: %d new fills since %d", new_count, since_ms)
        return new_count

    def _handle(self, address: str, msg: Any) -> None:
        if self.health is not None:
            self.health.touch()
        try:
            data = msg.get("data", msg) if isinstance(msg, dict) else {}
            fills = data.get("fills", []) or []
            is_snapshot = bool(data.get("isSnapshot", False))
            if is_snapshot:
                count = 0
                for f in fills:
                    try:
                        tid = f.get("tid") if isinstance(f, dict) else None
                        if tid is None:
                            continue
                        self.state.mark_tid_seen(int(tid), address)
                        count += 1
                    except (TypeError, ValueError):
                        log.warning("Bad tid in snapshot for %s: %r", address[:10], f)
                        continue
                log.info("Marked %d snapshot fills as seen for %s", count, address[:10])
                return
            for f in fills:
                try:
                    if not isinstance(f, dict):
                        continue
                    tid = f.get("tid")
                    if tid is None:
                        continue
                    if not self.state.mark_tid_seen(int(tid), address):
                        continue
                except (TypeError, ValueError):
                    log.warning("Bad tid in live fills for %s: %r", address[:10], f)
                    continue
                try:
                    self.on_fill(address, f)
                except Exception:
                    log.exception("on_fill handler raised for %s tid=%s", address[:10], tid)
        except Exception:
            log.exception("Follower error handling msg for %s", address[:10])
