import logging
import os
import time
from dataclasses import asdict, dataclass, replace
from threading import Lock

from .alerts import Alerter
from .config import Config
from .errors import OrderError
from .funding import FundingTracker
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


# In-flight TTL — how long after submit do we count an order toward exposure
# before assuming the own-fill WS feedback has updated PositionTracker. Set
# longer than typical HL WS round-trip (~100-500ms) but short enough that a
# stuck or rejected order doesn't permanently inflate our exposure estimate.
IN_FLIGHT_TTL_SECONDS = 30.0


class MirrorTrader:
    def __init__(
        self,
        cfg: Config,
        exchange: ExchangeProto,
        positions: PositionTracker,
        journal: Journal,
        alerter: Alerter,
        market_meta: MarketMeta,
        funding: FundingTracker | None = None,
    ):
        self.cfg = cfg
        self.exchange = exchange
        self.positions = positions
        self.journal = journal
        self.alerter = alerter
        self.market_meta = market_meta
        self.funding = funding  # None = funding-aware sizing disabled
        # Held across risk-check + submit so two concurrent leader fills can't
        # both pass the exposure cap based on stale state.
        self._submit_lock = Lock()
        # In-flight orders — pending notional that's been submitted but whose
        # own-fill confirmation hasn't yet propagated through PositionTracker.
        # Without this, rapid leader fills bypass `max_total_exposure_usd`
        # because each new submit's risk check sees stale local position state.
        # (Real-world bug: 19 fills bypassed a $60 cap and produced a 5x XMR
        # leverage runaway on 2026-05-05.) Each entry: (expires_at, notional).
        self._in_flight: list[tuple[float, float]] = []
        # Per-leader sizing weight, refreshed every discover_leaders cycle.
        # Default 1.0 = original proportional sizing. Updated via
        # update_leader_weights() from main's refresh loop.
        self._leader_weights: dict[str, float] = {}

    def update_leader_weights(self, weights: dict[str, float]) -> None:
        """Replace the per-leader weight map (called after each leader refresh).

        Keys are lowercase address strings; values are size multipliers in [0.1, 5.0].
        Missing leaders default to 1.0 in `_build_intent`.
        """
        self._leader_weights = {a.lower(): float(w) for a, w in weights.items()}

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
            intent = self._build_intent(fill, leader)
            if intent is None:
                self.journal.write("intent_skipped", leader=leader, tid=tid, reason="filter")
                return
            with self._submit_lock:
                # Re-evaluate reduce_only inside the lock — position state may
                # have changed (own-fill arrived) between _build_intent and here.
                intent = replace(
                    intent,
                    reduce_only=self._is_reduce_only(intent.coin, intent.is_buy, intent.sz),
                )
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
            # Unmark so backfill can re-dispatch this fill. OrderError stays
            # marked (the order was attempted; retrying could double-trade).
            if isinstance(tid, int):
                self.positions.state.unmark_tid_seen(tid)

    def _build_intent(self, fill: dict, leader: str = "") -> TradeIntent | None:
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
        weight = self._leader_weights.get(leader.lower(), 1.0) if leader else 1.0
        if s.mode == "proportional":
            mirror_notional = leader_notional * s.proportional_fraction * weight
        elif s.mode == "fixed":
            mirror_notional = float(s.fixed_usd) * weight
        else:
            return None

        # Outcomes have a separate (typically higher) min — HL enforces $10
        # USDH min on HIP-4 orders. Perps allow much smaller mirror sizes.
        is_outcome = coin.startswith("#") or coin.startswith("+")
        effective_min = (
            s.outcome_min_per_trade_usd
            if is_outcome and s.outcome_min_per_trade_usd is not None
            else s.min_per_trade_usd
        )

        # Funding-aware sizing (perps only — outcomes settle at expiry, no funding).
        # Apply BEFORE the max_per_trade cap so amplification still respects it.
        if (
            s.use_funding_aware_sizing
            and self.funding is not None
            and not is_outcome
        ):
            apr_pct = self.funding.get_apr_pct(coin)
            if apr_pct is not None:
                # Convention: APR > 0 means longs PAY shorts (i.e. shorts get paid).
                # we_get_paid_apr = +apr if short, -apr if long.
                we_get_paid_apr = -apr_pct if is_buy else apr_pct
                if we_get_paid_apr <= -s.funding_skip_threshold_apr_pct:
                    # Adverse funding too costly — skip the mirror entirely.
                    return None
                if we_get_paid_apr >= s.funding_amplify_threshold_apr_pct:
                    # Linear ramp: at threshold → 1.0x, scaled up to amplify_cap
                    # at +200% APR. Capped at amplify_cap.
                    raw_mult = 1.0 + (we_get_paid_apr / 200.0)
                    mirror_notional *= min(raw_mult, s.funding_amplify_cap)

        mirror_notional = min(mirror_notional, s.max_per_trade_usd)
        if mirror_notional < effective_min:
            return None

        raw_sz = mirror_notional / px
        rounded_sz = self.market_meta.round_size(coin, raw_sz)
        if rounded_sz <= 0:
            return None
        rounded_px = self.market_meta.round_price(px)
        rounded_notional = rounded_sz * rounded_px
        # Re-check min after rounding — szDecimals=0 outcomes can drop us below
        # the floor even though the raw notional was above it.
        if rounded_notional < effective_min:
            return None

        # reduce_only deferred — evaluated inside _submit_lock against fresh state.
        return TradeIntent(
            coin=coin,
            is_buy=is_buy,
            sz=rounded_sz,
            limit_px=rounded_px,
            notional_usd=rounded_notional,
            reduce_only=False,
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

    def _in_flight_notional(self) -> float:
        """Sum of in-flight order notionals whose TTL hasn't expired.

        Side-effect: prunes expired entries while iterating. Caller must hold
        `_submit_lock` (we do — _risk_check is called inside the lock).
        """
        now = time.time()
        active: list[tuple[float, float]] = []
        total = 0.0
        for expires_at, notional in self._in_flight:
            if expires_at > now:
                active.append((expires_at, notional))
                total += notional
        self._in_flight = active
        return total

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
        in_flight = self._in_flight_notional()
        # Total committed exposure = confirmed positions + pending submissions
        # whose own-fill hasn't propagated through PositionTracker yet. Without
        # `in_flight`, rapid-fire leader mirrors race the WS feedback loop and
        # bypass the cap entirely (real bug observed 2026-05-05).
        committed = exposure + in_flight
        if committed + intent.notional_usd > r.max_total_exposure_usd:
            return False, (
                f"exposure_cap (have=${exposure:.0f} + in_flight=${in_flight:.0f} "
                f"+ new=${intent.notional_usd:.0f} > ${r.max_total_exposure_usd:.0f})"
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
        # Track this order's notional as in-flight until own-fill confirms via
        # PositionTracker. _submit is called inside _submit_lock so it's safe
        # to mutate _in_flight here without an additional lock acquire.
        self._in_flight.append((time.time() + IN_FLIGHT_TTL_SECONDS, intent.notional_usd))
        log.info("Order result: %s", result)
        self.journal.write(
            "order_result",
            leader=leader,
            tid=tid,
            intent=asdict(intent),
            result=result,
        )
