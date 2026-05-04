import logging
import os
from dataclasses import asdict, dataclass
from threading import Lock

from .alerts import Alerter
from .config import Config
from .errors import OrderError
from .journal import Journal
from .market_meta import MarketMeta
from .positions import PositionTracker
from .protocols import ExchangeProto

log = logging.getLogger(__name__)


@dataclass
class TradeIntent:
    coin: str
    is_buy: bool
    sz: float
    limit_px: float
    notional_usd: float
    reduce_only: bool = False


class MirrorTrader:
    def __init__(
        self,
        cfg: Config,
        exchange: ExchangeProto,
        positions: PositionTracker,
        journal: Journal,
        alerter: Alerter,
        market_meta: MarketMeta,
    ):
        self.cfg = cfg
        self.exchange = exchange
        self.positions = positions
        self.journal = journal
        self.alerter = alerter
        self.market_meta = market_meta
        # Held across risk-check + submit so two concurrent leader fills can't
        # both pass the exposure cap based on stale state.
        self._submit_lock = Lock()

    def on_leader_fill(self, leader: str, fill: dict) -> None:
        tid = fill.get("tid")
        try:
            self.journal.write(
                "leader_fill",
                leader=leader,
                tid=tid,
                coin=fill.get("coin"),
                px=fill.get("px"),
                sz=fill.get("sz"),
                side=fill.get("side"),
            )
            intent = self._build_intent(fill)
            if intent is None:
                self.journal.write("intent_skipped", leader=leader, tid=tid, reason="filter")
                return
            with self._submit_lock:
                ok, reason = self._risk_check(intent)
                self.journal.write(
                    "risk_check",
                    leader=leader,
                    tid=tid,
                    ok=ok,
                    reason=reason,
                    intent=asdict(intent),
                )
                if not ok:
                    log.info(
                        "[risk] reject (%s) leader=%s coin=%s", reason, leader[:10], intent.coin
                    )
                    return
                self._submit(intent, leader, tid)
        except OrderError:
            raise
        except Exception:
            log.exception("Mirror pipeline error leader=%s tid=%s", leader, tid)
            self.alerter.alert(
                "error",
                f"Mirror pipeline exception leader={leader[:10]} tid={tid}",
            )
            self.journal.write("pipeline_error", leader=leader, tid=tid)

    def _build_intent(self, fill: dict) -> TradeIntent | None:
        coin = fill.get("coin")
        try:
            px = float(fill.get("px", 0))
            sz = float(fill.get("sz", 0))
        except (TypeError, ValueError):
            return None
        side = fill.get("side")
        if not coin or px <= 0 or sz <= 0 or side not in ("B", "A"):
            return None
        if not self._is_allowed_market(coin):
            return None

        is_buy = side == "B"
        leader_notional = px * sz
        s = self.cfg.sizing
        if s.mode == "proportional":
            mirror_notional = leader_notional * s.proportional_fraction
        elif s.mode == "fixed":
            mirror_notional = float(s.fixed_usd)
        else:
            return None

        mirror_notional = min(mirror_notional, s.max_per_trade_usd)
        if mirror_notional < s.min_per_trade_usd:
            return None

        raw_sz = mirror_notional / px
        rounded_sz = self.market_meta.round_size(coin, raw_sz)
        if rounded_sz <= 0:
            return None
        rounded_px = self.market_meta.round_price(px)
        rounded_notional = rounded_sz * rounded_px
        # Re-check min after rounding — szDecimals=0 outcomes can drop us below
        # the floor even though the raw notional was above it.
        if rounded_notional < s.min_per_trade_usd:
            return None

        reduce_only = self._is_reduce_only(coin, is_buy, rounded_sz)
        return TradeIntent(
            coin=coin,
            is_buy=is_buy,
            sz=rounded_sz,
            limit_px=rounded_px,
            notional_usd=rounded_notional,
            reduce_only=reduce_only,
        )

    def _is_reduce_only(self, coin: str, is_buy: bool, sz: float) -> bool:
        """True iff this order strictly shrinks an existing opposing position
        without flipping through zero. HL rejects reduce-only orders that flip.
        """
        existing_sz, _ = self.positions.state.get_position(coin)
        if existing_sz == 0:
            return False
        # Long position + sell, or short position + buy → reducing
        opposing = (existing_sz > 0 and not is_buy) or (existing_sz < 0 and is_buy)
        if not opposing:
            return False
        return sz <= abs(existing_sz)

    def _is_allowed_market(self, coin: str) -> bool:
        allowed = self.cfg.risk.allowed_market_types
        is_outcome = coin.startswith("#") or coin.startswith("+")
        is_spot = coin.startswith("@") or "/" in coin
        is_perp = not is_outcome and not is_spot
        return (
            (is_outcome and "outcome" in allowed)
            or (is_spot and "spot" in allowed)
            or (is_perp and "perp" in allowed)
        )

    def _risk_check(self, intent: TradeIntent) -> tuple[bool, str]:
        r = self.cfg.risk
        if os.path.exists(r.kill_switch_file):
            self.alerter.alert("warn", f"Kill switch active: {r.kill_switch_file}")
            return False, "kill_switch"

        net_realized = self.positions.realized_pnl_today()
        if -net_realized >= r.max_daily_loss_usd:
            self.alerter.alert("critical", f"Daily loss cap hit: net=${net_realized:.2f}")
            return False, f"daily_loss_cap (net={net_realized:.2f})"

        # Reduce-only orders shrink, never grow exposure — bypass the cap.
        if intent.reduce_only:
            return True, ""

        exposure = self.positions.total_exposure_usd()
        if exposure + intent.notional_usd > r.max_total_exposure_usd:
            return False, (
                f"exposure_cap (have=${exposure:.0f} + new=${intent.notional_usd:.0f} "
                f"> ${r.max_total_exposure_usd:.0f})"
            )
        return True, ""

    def _submit(self, intent: TradeIntent, leader: str, tid: object) -> None:
        if self.cfg.risk.dry_run:
            log.info(
                "[DRY] %s %s %.6f @ %.4f notional=$%.2f reduce_only=%s leader=%s tid=%s",
                "BUY" if intent.is_buy else "SELL",
                intent.coin,
                intent.sz,
                intent.limit_px,
                intent.notional_usd,
                intent.reduce_only,
                leader[:10],
                tid,
            )
            self.journal.write("order_dry_run", leader=leader, tid=tid, intent=asdict(intent))
            return

        slip = self.cfg.sizing.ioc_slippage_bps / 10_000.0
        slipped_px = intent.limit_px * (1 + slip if intent.is_buy else 1 - slip)
        px = self.market_meta.round_price(slipped_px)
        log.info(
            "Submitting %s %s %.6f @ %.4f notional=$%.2f reduce_only=%s",
            "BUY" if intent.is_buy else "SELL",
            intent.coin,
            intent.sz,
            px,
            intent.notional_usd,
            intent.reduce_only,
        )
        try:
            result = self.exchange.order(
                intent.coin,
                intent.is_buy,
                intent.sz,
                px,
                order_type={"limit": {"tif": "Ioc"}},
                reduce_only=intent.reduce_only,
            )
        except Exception as e:
            self.alerter.alert("error", f"Order submit failed: {type(e).__name__}: {e}")
            self.journal.write(
                "order_failed",
                leader=leader,
                tid=tid,
                intent=asdict(intent),
                error=str(e),
            )
            raise OrderError(f"order failed: {e}") from e
        log.info("Order result: %s", result)
        self.journal.write(
            "order_result",
            leader=leader,
            tid=tid,
            intent=asdict(intent),
            result=result,
        )
