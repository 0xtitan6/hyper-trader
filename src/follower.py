import logging
import threading
from collections.abc import Callable
from typing import Any

from .connection import ConnectionHealth
from .state import State

log = logging.getLogger(__name__)

FillCallback = Callable[[str, dict], None]


class FillFollower:
    """Subscribes to leader userFills WS, dedupes by tid via persistent state,
    forwards new fills to a callback.
    """

    def __init__(
        self,
        info: Any,
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
                    tid = f.get("tid")
                    if tid is not None:
                        self.state.mark_tid_seen(int(tid), address)
                        count += 1
                log.info("Marked %d snapshot fills as seen for %s", count, address[:10])
                return
            for f in fills:
                tid = f.get("tid")
                if tid is None:
                    continue
                if not self.state.mark_tid_seen(int(tid), address):
                    continue
                self.on_fill(address, f)
        except Exception:
            log.exception("Follower error handling msg for %s", address[:10])
