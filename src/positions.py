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
        with self._lock:
            if self._subscribed:
                return
            log.info("Subscribing to own userFills for %s", self.account_address[:10])
            self.info.subscribe(
                {"type": "userFills", "user": self.account_address},
                self._handle,
            )
            self._subscribed = True

    def realized_pnl_today(self) -> float:
        """Net of fees: closedPnl - fee. This is what the daily-loss kill switch checks."""
        gross, fee = self.state.daily_pnl(today_utc())
        return gross - fee

    def realized_pnl_today_gross(self) -> float:
        """Gross PnL only, no fees deducted. Useful for journaling / debugging."""
        return self.state.daily_pnl(today_utc())[0]

    def total_exposure_usd(self) -> float:
        total = 0.0
        for _coin, (sz, avg_px) in self.state.get_positions().items():
            total += abs(sz) * avg_px
        return total

    def reconcile_with_user_state(self) -> dict[str, tuple[float, float]]:
        """Overwrite local position state with HL's authoritative `user_state`.

        Run at startup (before the WS snapshot, which can be truncated) and
        periodically (so HIP-4 settlement, manual trades, and any drift get
        picked up). Returns the post-reconcile map of {coin: (sz, avg_px)}.

        Coins that exist locally but not upstream are zeroed — that's how
        settled outcome positions get cleared.
        """
        try:
            us = self.info.user_state(self.account_address) or {}
        except Exception:
            log.exception("reconcile: user_state fetch failed; keeping local state")
            return self.state.get_positions()

        upstream: dict[str, tuple[float, float]] = {}
        for ap in us.get("assetPositions", []) or []:
            pos = ap.get("position") if isinstance(ap, dict) else None
            if not isinstance(pos, dict):
                continue
            coin = pos.get("coin")
            if not coin:
                continue
            try:
                szi = float(pos.get("szi", 0))
                entry_px = float(pos.get("entryPx", 0) or 0)
            except (TypeError, ValueError):
                log.warning("reconcile: malformed position %s", pos)
                continue
            upstream[coin] = (szi, entry_px)

        local = self.state.get_positions()
        with self._lock:
            for coin, (sz, avg_px) in upstream.items():
                cur = local.get(coin)
                if cur != (sz, avg_px):
                    log.info(
                        "reconcile: %s local=%s upstream=(%s,%s)",
                        coin,
                        cur,
                        sz,
                        avg_px,
                    )
                self.state.update_position(coin, sz, avg_px)
            for coin in local.keys() - upstream.keys():
                cur_sz, _cur_avg = local[coin]
                if cur_sz != 0:
                    log.info("reconcile: zeroing %s (was sz=%s)", coin, cur_sz)
                    self.state.update_position(coin, 0.0, 0.0)
        self.journal.write(
            "reconcile",
            upstream_count=len(upstream),
            local_count=len(local),
            zeroed=sorted(local.keys() - upstream.keys()),
        )
        return self.state.get_positions()

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
