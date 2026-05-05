import logging
import time
from datetime import UTC, datetime
from threading import RLock
from typing import Any

from .alerts import Alerter, NullAlerter
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
        alerter: Alerter | None = None,
    ):
        self.info = info
        self.account_address = account_address.lower()
        self.state = state
        self.journal = journal
        self.health = health
        self.alerter: Alerter = alerter or NullAlerter()
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
        """Overwrite local position state with HL's authoritative state.

        Queries BOTH:
          - `user_state` for perp/futures `assetPositions`
          - `spotClearinghouseState` for HIP-4 outcome positions (`+NN` coins
            with positive balance — these are NOT in `assetPositions`)

        Run at startup (before the WS snapshot, which can be truncated) and
        periodically (so HIP-4 settlement, manual trades, and any drift get
        picked up). Returns the post-reconcile map of {coin: (sz, avg_px)}.

        Coins that exist locally but not upstream are zeroed — that's how
        settled outcome positions get cleared.
        """
        upstream: dict[str, tuple[float, float]] = {}

        try:
            us = self.info.user_state(self.account_address) or {}
        except Exception:
            log.exception("reconcile: user_state fetch failed; keeping local state")
            return self.state.get_positions()

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

        # HIP-4 outcomes live in spotClearinghouseState.balances as `+NN`,
        # NOT in assetPositions. Without this, our local outcome positions
        # would get zeroed every reconcile cycle (treated as "missing
        # upstream") even though we still own them.
        try:
            sc = (
                self.info.post(
                    "/info",
                    {"type": "spotClearinghouseState", "user": self.account_address},
                )
                or {}
            )
        except Exception:
            log.exception("reconcile: spotClearinghouseState fetch failed; perp-only")
            sc = {}
        for b in sc.get("balances", []) or []:
            if not isinstance(b, dict):
                continue
            coin = b.get("coin")
            if not coin or not coin.startswith("+"):
                continue  # only outcome legs (USDC/USDH/etc are not positions)
            try:
                total = float(b.get("total", 0) or 0)
                entry_ntl = float(b.get("entryNtl", 0) or 0)
            except (TypeError, ValueError):
                log.warning("reconcile: malformed spot balance %s", b)
                continue
            if total <= 0:
                continue
            avg_px = entry_ntl / total if total > 0 else 0.0
            # Translate `+NN` (spot ticker) to `#NN` (trade ticker) — they're
            # the same outcome leg but HL uses different prefixes per surface.
            trade_coin = "#" + coin[1:]
            upstream[trade_coin] = (total, avg_px)

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
        direction = fill.get("dir", "")
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

        # Settlement is a special HL fill type for HIP-4 outcomes: px=0 (settled
        # to zero) or px=1 (settled to one), dir="Settlement". Pre-fix this code
        # rejected px=0 fills as malformed, leaving the position dangling and
        # missing the closed_pnl. Now we handle settlement explicitly.
        is_settlement = direction == "Settlement"
        if is_settlement:
            if not coin or sz <= 0:
                log.warning("Malformed settlement fill (fields): %s", fill)
                return
            self._handle_settlement(
                tid=int(tid),
                coin=coin,
                sz=sz,
                px=px,
                closed_pnl=closed_pnl,
                fee=fee,
                ts=ts,
                date_utc=date_utc,
                is_snapshot=is_snapshot,
                fill=fill,
            )
            return

        if not coin or side not in ("B", "A") or sz <= 0 or px <= 0:
            log.warning("Malformed own fill (fields): %s", fill)
            return

        # Skip HL spot-pair fills (`@NNN` or contain `/`). These are stablecoin
        # swaps and other spot trades — they're EXCHANGES, not positions. If we
        # tracked them as positions, the inventory would falsely count toward
        # exposure caps and block legitimate trades. (Real-world bug: a one-time
        # USDC→USDH swap registered as "@230 short of 54.73" and blocked all
        # perp mirroring until reconcile.)
        is_spot_pair = coin.startswith("@") or "/" in coin
        if is_spot_pair:
            self.journal.write(
                "own_fill_skipped",
                tid=int(tid),
                coin=coin,
                reason="spot_pair_not_a_position",
                sz=sz,
                px=px,
            )
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

    def _handle_settlement(
        self,
        *,
        tid: int,
        coin: str,
        sz: float,
        px: float,
        closed_pnl: float,
        fee: float,
        ts: int,
        date_utc: str,
        is_snapshot: bool,
        fill: dict,
    ) -> None:
        """HIP-4 outcome settlement event.

        - Records the fill (so daily P&L includes the realized closed_pnl).
        - Zeros our local position for this coin (it's literally gone).
        - Emits a critical alert with the verdict + P&L (so operator gets
          notified via webhook even if the agent supervising the trade is
          offline — solves the "session went silent at expiry" failure mode).
        - Journals 'settlement' explicitly for forensics.
        """
        with self._lock:
            inserted = self.state.record_own_fill(
                tid=tid,
                coin=coin,
                side=fill.get("side") or "A",
                sz=sz,
                px=px,
                closed_pnl=closed_pnl,
                fee=fee,
                ts=ts,
                date_utc=date_utc,
            )
            if inserted:
                self.state.update_position(coin, 0.0, 0.0)
        verdict = "won" if closed_pnl > 0 else ("lost" if closed_pnl < 0 else "flat")
        sign = "+" if closed_pnl >= 0 else ""
        msg = (
            f"SETTLEMENT {coin} {sz:g} shares -> px=${px:.4f} "
            f"({verdict}, pnl={sign}${closed_pnl:.2f})"
        )
        log.warning(msg)
        self.alerter.alert("critical", msg)
        self.journal.write(
            "settlement",
            tid=tid,
            coin=coin,
            sz=sz,
            settle_px=px,
            closed_pnl=closed_pnl,
            fee=fee,
            verdict=verdict,
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
