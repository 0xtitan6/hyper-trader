import logging
import time
from datetime import UTC, datetime
from threading import RLock
from typing import Any

from .connection import ConnectionHealth
from .journal import Journal
from .protocols import InfoProto
from .state import State

log = logging.getLogger(__name__)


def today_utc() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%d")


class PositionTracker:
    """Subscribes to our own userFills, persists fills, maintains positions and
    realized PnL. Source of truth for the daily-loss kill switch and exposure cap.

    Exposure is computed at cost basis (|sz| * avg_px). For HIP-4 outcomes that's
    the actual max loss; for perps it's an approximation that ignores leverage.
    """

    def __init__(
        self,
        info: InfoProto,
        account_address: str,
        state: State,
        journal: Journal,
        health: ConnectionHealth | None = None,
    ):
        self.info = info
        self.account_address = account_address.lower()
        self.state = state
        self.journal = journal
        self.health = health
        self._lock = RLock()
        self._subscribed = False

    def start(self) -> None:
        if self._subscribed:
            return
        log.info("Subscribing to own userFills for %s", self.account_address[:10])
        self.info.subscribe(
            {"type": "userFills", "user": self.account_address},
            self._handle,
        )
        self._subscribed = True

    def realized_pnl_today(self) -> float:
        return self.state.daily_pnl(today_utc())[0]

    def total_exposure_usd(self) -> float:
        total = 0.0
        for _coin, (sz, avg_px) in self.state.get_positions().items():
            total += abs(sz) * avg_px
        return total

    def _handle(self, msg: Any) -> None:
        if self.health is not None:
            self.health.touch()
        try:
            data = msg.get("data", msg) if isinstance(msg, dict) else {}
            fills = data.get("fills", []) or []
            is_snapshot = bool(data.get("isSnapshot", False))
            for f in fills:
                self._on_fill(f, is_snapshot=is_snapshot)
        except Exception:
            log.exception("PositionTracker error handling msg")

    def _on_fill(self, fill: dict, is_snapshot: bool) -> None:
        tid = fill.get("tid")
        if tid is None:
            return
        coin = fill.get("coin")
        side = fill.get("side")
        try:
            sz = float(fill.get("sz", 0))
            px = float(fill.get("px", 0))
            closed_pnl = float(fill.get("closedPnl", 0))
            fee = float(fill.get("fee", 0))
        except (TypeError, ValueError):
            log.warning("Malformed own fill (numeric): %s", fill)
            return
        ts = int(fill.get("time", time.time() * 1000)) // 1000
        date_utc = datetime.fromtimestamp(ts, tz=UTC).strftime("%Y-%m-%d")

        if not coin or side not in ("B", "A") or sz <= 0 or px <= 0:
            log.warning("Malformed own fill (fields): %s", fill)
            return

        with self._lock:
            inserted = self.state.record_own_fill(
                tid=int(tid),
                coin=coin,
                side=side,
                sz=sz,
                px=px,
                closed_pnl=closed_pnl,
                fee=fee,
                ts=ts,
                date_utc=date_utc,
            )
            if not inserted:
                return  # dup
            self._update_position(coin=coin, side=side, sz=sz, px=px)

        self.journal.write(
            "own_fill",
            tid=int(tid),
            coin=coin,
            side=side,
            sz=sz,
            px=px,
            closed_pnl=closed_pnl,
            fee=fee,
            is_snapshot=is_snapshot,
        )

    def _update_position(self, coin: str, side: str, sz: float, px: float) -> None:
        delta = sz if side == "B" else -sz
        existing_sz, existing_avg = self.state.get_position(coin)
        new_sz = existing_sz + delta
        new_avg: float
        if existing_sz == 0:
            new_avg = px
        elif (existing_sz > 0) == (delta > 0):
            new_avg = ((existing_sz * existing_avg) + (delta * px)) / new_sz if new_sz != 0 else 0.0
        elif abs(delta) >= abs(existing_sz):
            new_avg = px if new_sz != 0 else 0.0
        else:
            new_avg = existing_avg
        self.state.update_position(coin, new_sz, new_avg)
