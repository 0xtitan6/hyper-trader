import logging
import os
import time
from dataclasses import asdict, dataclass, replace
from threading import Lock

from hyperliquid.utils.error import ClientError

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

# Order submit retry config. HL rate-limits the /exchange endpoint per-account
# at sub-second granularity; a leader firing 5+ child orders in 1s can trip
# the limiter (live cost 2026-05-30: 6 missed fills today from one ZEC burst).
# Only retry on 429 — other exceptions are ambiguous (timeout-mid-request
# might have placed the order). 2 retries with exp backoff caps total latency
# at ~3s in the worst case.
ORDER_RETRY_MAX_ATTEMPTS = 3   # 1 initial + 2 retries
ORDER_RETRY_BASE_BACKOFF_S = 1.0

# HL rejects some orders IN-BAND: HTTP 200 with the error nested in the body
# ({'status':'ok','response':{'data':{'statuses':[{'error':'Order has invalid
# size.'}]}}}). These never rest or fill. Two failures follow if we ignore it:
#   1. in_flight leak — we reserve notional that only clears on TTL, leaking
#      phantom exposure that eats the cap (observed 2026-06-29: $440 phantom).
#   2. API storm — the HIP-3 `xyz:` equity perps reject EVERY open on szDecimals
#      ("invalid size"), and the mirror re-fires per leader child-fill, so one
#      leader burst becomes hundreds of doomed submits (668 rejects in 4h).
# Structural per-coin errors below get the coin cooled down; others just skip.
POISON_COOLDOWN_SECONDS = 300.0
_POISON_ORDER_ERRORS = ("invalid size", "invalid price")


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
        # Coins under a structural-rejection cooldown (bad szDecimals → repeated
        # "invalid size"). coin -> unix ts until which new opens are skipped.
        self._poison_until: dict[str, float] = {}
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
                # Per-coin weight-priority conflict lock (PR #25). Caught
                # 2026-05-10: two leaders took opposite sides on TON within
                # 30 min and we whipsawed -$1.85 across both legs. Rule:
                # if our existing position on this coin was opened by a
                # different leader AND new fill is opposite-direction AND
                # current leader has lower weight than originator → skip.
                conflict_reason = self._check_leader_conflict(intent, leader)
                if conflict_reason is not None:
                    log.info(
                        "[conflict] skip leader=%s coin=%s reason=%s",
                        leader[:10], intent.coin, conflict_reason,
                    )
                    self.journal.write(
                        "intent_skipped", leader=leader, tid=tid,
                        reason=f"leader_conflict:{conflict_reason}",
                    )
                    return
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
                submitted = self._submit(intent, leader, tid)
                # Record this leader as the position's originator only if the
                # order was actually accepted — a rejected order must not claim
                # the coin (else conflict-lock blocks the real originator).
                if submitted:
                    self.positions.state.set_position_originator(intent.coin, leader)
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

    def _check_leader_conflict(self, intent: TradeIntent, leader: str) -> str | None:
        """Per-coin weight-priority conflict check.

        Returns None if the trade is allowed, or a string reason if it should
        be skipped because a different (higher-weight) leader already holds
        a position on this coin in the opposite direction.

        Rules:
          1. No existing position → allow (this leader becomes originator)
          2. Existing position from same leader → allow (they're managing it)
          3. Existing position from different leader, same direction → allow
             (we're adding to position they opened; benign)
          4. Existing position from different leader, OPPOSITE direction
             AND new leader weight ≤ originator weight → SKIP (conflict)
          5. Same as (4) but new leader has STRICTLY higher weight → allow
             (override based on conviction)
        """
        existing_sz, _ = self.positions.state.get_position(intent.coin)
        if existing_sz == 0:
            return None  # rule 1
        originator = self.positions.state.get_position_originator(intent.coin)
        # Treat anything that isn't a real address string as "unset" — covers
        # pre-PR-#25 positions migrated without an originator AND defends
        # against mock-test environments returning unexpected types.
        if not isinstance(originator, str) or not originator:
            return None
        if originator == leader.lower():
            return None  # rule 2: same leader managing their own position
        # Different leader. Is the new fill opposite-direction?
        opposing = (existing_sz > 0 and not intent.is_buy) or (
            existing_sz < 0 and intent.is_buy
        )
        if not opposing:
            return None  # rule 3 (same-side add is fine)
        # Conflict candidate. Compare weights.
        new_weight = self._leader_weights.get(leader.lower(), 1.0)
        orig_weight = self._leader_weights.get(originator, 1.0)
        if new_weight > orig_weight:
            return None  # rule 5 (override allowed)
        # rule 4: skip
        return (
            f"originator={originator[:10]} (w={orig_weight:.2f}) "
            f"vs new_leader={leader[:10]} (w={new_weight:.2f})"
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
        # (Also bypasses the poison cooldown below: exits must always be allowed
        # so a poisoned coin can still be closed if we somehow hold it.)
        if intent.reduce_only:
            return True, ""

        # Coin in structural-rejection cooldown (see _POISON_ORDER_ERRORS). Skip
        # opens so we don't storm the API with orders that can't succeed.
        if self._poison_until.get(intent.coin, 0.0) > time.time():
            return False, f"poison_cooldown ({intent.coin})"

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

    def _submit_with_retry(
        self, intent: TradeIntent, px: float, leader: str, tid: object
    ) -> dict:
        """Wrap exchange.order() with retry on 429. Caller is `_submit` —
        runs inside `_submit_lock` so no concurrent retry storms.

        Why only 429: HL responds 429 BEFORE the order is placed (rejected at
        the rate-limit gate), so retry is safe — no risk of double-submit.
        Network timeouts mid-request, by contrast, are ambiguous (order may
        or may not have reached the matching engine), so we fail loud and
        let the operator decide.

        On final failure, raises OrderError exactly like the original
        no-retry path — caller-side journal+alert behavior is unchanged.
        """
        last_exc: Exception | None = None
        for attempt in range(ORDER_RETRY_MAX_ATTEMPTS):
            try:
                result = self.exchange.order(
                    intent.coin,
                    intent.is_buy,
                    intent.sz,
                    px,
                    order_type={"limit": {"tif": "Ioc"}},
                    reduce_only=intent.reduce_only,
                )
            except ClientError as e:
                last_exc = e
                status = getattr(e, "status_code", None)
                # Some wrappers stash the code as args[0]
                if status is None and e.args:
                    candidate = e.args[0]
                    if isinstance(candidate, int):
                        status = candidate
                if status == 429 and attempt + 1 < ORDER_RETRY_MAX_ATTEMPTS:
                    backoff = ORDER_RETRY_BASE_BACKOFF_S * (2**attempt)
                    log.warning(
                        "Order 429 on %s (attempt %d/%d); sleeping %.1fs",
                        intent.coin, attempt + 1, ORDER_RETRY_MAX_ATTEMPTS, backoff,
                    )
                    time.sleep(backoff)
                    continue
                # Non-retryable status (or budget exhausted) — fall through to raise
                break
            except Exception as e:
                # Non-ClientError: ambiguous (timeout, connection) → fail fast.
                # Don't retry — could double-submit a placed order.
                last_exc = e
                break
            else:
                # Success — caller (_submit) handles in-flight tracking +
                # journaling.
                return result

        # All attempts exhausted or non-retryable failure
        assert last_exc is not None
        self.alerter.alert(
            "error", f"Order submit failed: {type(last_exc).__name__}: {last_exc}"
        )
        self.journal.write(
            "order_failed",
            leader=leader,
            tid=tid,
            intent=asdict(intent),
            error=str(last_exc),
        )
        raise OrderError(f"order failed: {last_exc}") from last_exc

    @staticmethod
    def _order_status_error(result: object) -> str | None:
        """Return HL's in-band rejection message, or None if the order was
        accepted (rested or filled).

        HL returns HTTP 200 even when it rejects an order — the reason is nested
        in the body. Two shapes:
          - {'status': 'err', 'response': '<message>'}
          - {'status': 'ok', 'response': {'data': {'statuses': [{'error': ...}]}}}
        A status dict with 'resting' or 'filled' (and no 'error') is a success.
        """
        if not isinstance(result, dict):
            return None
        if result.get("status") == "err":
            resp = result.get("response")
            return str(resp) if resp else "unknown error"
        response = result.get("response")
        if not isinstance(response, dict):
            return None
        data = response.get("data")
        if not isinstance(data, dict):
            return None
        for st in data.get("statuses") or []:
            if isinstance(st, dict) and st.get("error"):
                return str(st["error"])
        return None

    def _submit(self, intent: TradeIntent, leader: str, tid: object) -> bool:
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
            return True

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
        result = self._submit_with_retry(intent, px, leader, tid)
        log.info("Order result: %s", result)

        # HL may reject in-band (HTTP 200 + error in body). A rejected order
        # never rests or fills, so it must NOT reserve in-flight notional —
        # doing so leaks phantom exposure that only clears on TTL (2026-06-29:
        # $440 phantom ate the cap). Return False so the caller doesn't claim
        # the coin's originator slot.
        err = self._order_status_error(result)
        if err is not None:
            log.warning("Order rejected in-band on %s: %s", intent.coin, err)
            self.alerter.alert("warn", f"Order rejected {intent.coin}: {err}")
            self.journal.write(
                "order_rejected",
                leader=leader,
                tid=tid,
                intent=asdict(intent),
                error=err,
                result=result,
            )
            if any(p in err.lower() for p in _POISON_ORDER_ERRORS):
                self._poison_until[intent.coin] = time.time() + POISON_COOLDOWN_SECONDS
                log.warning(
                    "Coin %s poisoned for %.0fs (%s)",
                    intent.coin, POISON_COOLDOWN_SECONDS, err,
                )
            return False

        # Accepted (rested/filled): reserve notional as in-flight until the
        # own-fill confirmation propagates through PositionTracker. _submit runs
        # inside _submit_lock so mutating _in_flight here needs no extra lock.
        self._in_flight.append((time.time() + IN_FLIGHT_TTL_SECONDS, intent.notional_usd))
        self.journal.write(
            "order_result",
            leader=leader,
            tid=tid,
            intent=asdict(intent),
            result=result,
        )
        return True
