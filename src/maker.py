"""Outcome market-maker for HL HIP-4.

Posts paired bid/ask quotes around mid on a configured outcome leg, captures
spread on round-trips, skews quotes toward zero based on inventory. Conservative
by design: defaults to refusing to quote when spread floor isn't met (so we
never make negative-EV markets after fees).

## Fee math

HL HIP-4 charges zero fee on OPEN, small fee (~0.015% round-trip) on CLOSE.
For a maker round-trip (buy-on-bid then sell-on-ask, both post-only):

    1. Someone hits our bid → we buy at bid, open a long → 0 fee
    2. Someone hits our ask → we sell at ask, close the long → ~0.0075% fee
    Captured: (ask - bid) * sz - 0.0075% * ask * sz

For positive EV, ask - bid (spread) must exceed ~0.0075% * mid_px (one-side
close fee). On a $0.60 share, that's ~$0.000045 ≈ 0.075 bps. Tight but doable.

The conservative `min_spread_bps` config defaults to **30 bps** (way above the
fee floor) so we only quote markets where edge is real. Operator can lower it.

## Safety

  - post-only (`tif: "Alo"`) — we never take, never pay slippage
  - hard inventory cap per side (default $5)
  - hard total exposure cap (default $20)
  - never quotes if spread < min_spread_bps OR mid invalid OR near expiry
  - respects KILL switch
  - sanity bounds on quote prices (0.01-0.99)
"""

from __future__ import annotations

import json
import logging
import math
import os
import statistics
import time
from collections import deque
from dataclasses import dataclass, field
from threading import Lock
from typing import Any

from .errors import OrderError
from .journal import Journal
from .market_meta import MarketMeta
from .protocols import ExchangeProto, InfoProto

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class MakerConfig:
    """Per-market configuration. One MakerConfig drives one outcome leg."""

    coin: str  # e.g. "#20" or "#21"
    expiry_ts: int
    # Quote sizing
    quote_size_shares: float = 1.0  # shares per quote-side
    # Spread floor — only post when (best_ask - best_bid) ≥ this
    min_spread_bps: float = 30.0  # 0.30% — well above the ~0.75bp close-fee floor
    # Quote placement: how far from the mid we sit (in ticks below/above)
    quote_offset_ticks: int = 1  # 1 tick inside best bid/ask, or at mid if book empty
    # Risk caps
    max_position_shares: float = 20.0
    max_inventory_usd: float = 5.0
    # Inventory skew — when long, we lean the quote pair down to encourage
    # selling. Two skew models are available:
    #   - GLFT (default, `use_glft_skew=True`): vol-aware Guéant-Lehalle-
    #     Fernandez-Tapia skew. The quote pair is walked DOWN by
    #     `q * sigma_eff * c2` where q is normalized inventory, sigma_eff the
    #     Bernoulli-adjusted realized volatility, and c2 a γ/A/k coefficient.
    #   - Legacy linear (`use_glft_skew=False`): the old crude
    #     `(inventory / max_position) * skew_bps_at_full * mid` form.
    use_glft_skew: bool = True
    # Legacy-linear skew strength. Unused on the GLFT path; kept for backward
    # compat so older configs/tests can pin the linear behavior.
    inventory_skew_bps_at_full: float = 20.0
    # GLFT parameters (ported from polymarket-mm configs/config.yaml:
    # γ=0.40, k=1.5). `glft_A` is the A-S liquidity/arrival constant.
    gamma: float = 0.40  # risk aversion
    glft_k: float = 1.5  # order-arrival decay
    glft_A: float = 1.0  # order-arrival intensity scale
    # Volatility estimator. The outcome price lives in [0,1], so we use
    # ARITHMETIC (not log) returns and a Bernoulli variance adjustment.
    # sigma_base is the fallback per-tick stdev of mid before the rolling
    # window has enough samples — a small value for a [0,1] price (here 2% of
    # a unit price ≈ 0.02 absolute move per ~2s tick).
    sigma_base: float = 0.02
    sigma_window: int = 90  # rolling mids kept (~3 min at 2s ticks)
    # Cancel-replace cadence
    cancel_threshold_bps: float = 5.0  # cancel + replace if mid moves ≥ this
    refresh_interval_s: float = 2.0  # min time between cancel+replace cycles
    # Timing
    expiry_buffer_s: int = 300  # stop quoting this many seconds before expiry
    # Sanity
    min_quote_px: float = 0.01
    max_quote_px: float = 0.99
    kill_switch_file: str = "./KILL"
    # Match-event gating. A goal in a live World Cup match can reprice a WINNER
    # leg faster than the 2s REST poll can cancel → adverse selection. v1 uses a
    # STATIC fixture calendar (no live feed) and gates on ANY live match, since
    # WINNER markets reprice on any team's result. While `now` falls inside a
    # fixture window the maker pulls all quotes and refuses to post.
    match_gate_enabled: bool = False
    match_gate_preroll_s: int = 120  # stop quoting this many seconds before kickoff
    match_gate_cooldown_s: int = 300  # resume this many seconds after est_end
    match_gate_fixtures_file: str = ""  # JSON path; "" → use inline list
    # Inline fixtures (tuple because the cfg is frozen). Each is a dict:
    #   {"match_id": str, "teams": [str, str], "kickoff_ts": int, "est_end_ts": int}
    match_gate_fixtures: tuple = ()
    # ------- Favorite-longshot "NO-skew" edge -------
    # Kalshi/sports favorite-longshot bias: longshots are overpriced, so leaning
    # NO on cheap longshot legs is a documented (but thin, tail-risky) edge. We
    # apply a price-GRADED downward push on YES fair value, scaled by the YES
    # probability `p`, and circuit-break it when a leg surges (dark-horse). All
    # defaults are NO-OP/off so existing behavior is byte-identical.
    noskew_enabled: bool = False
    # Probability band over which the edge is live. Below `p_floor` is noise;
    # above `p_cap` we're on the favorite side where the bias reverses → zero.
    noskew_p_floor: float = 0.02
    noskew_p_cap: float = 0.50
    # Piecewise-linear magnitude (bps of fair-value shift). Two segments tied at
    # the knee: a steep ramp on [p_floor, knee_p] from bps_at_floor → bps_at_knee,
    # then a shallow ramp on (knee_p, p_cap] from bps_at_knee → 0.
    noskew_bps_at_floor: float = 250.0
    noskew_bps_at_knee: float = 100.0
    noskew_knee_p: float = 0.10
    # Dark-horse circuit-breaker: neutralize the skew when current p has surged
    # to >= this multiple of the recent rolling minimum p (default 2x = doubled).
    noskew_surge_mult: float = 2.0
    # Hard cap on the COMBINED directional offset (GLFT skew + NO-skew), as a
    # fraction of mid, so neither lean can walk a quote across the book.
    combined_skew_cap_frac: float = 0.0050
    # ------- Toxicity / flow-imbalance defense -------
    # A burst of one-sided aggressive flow (informed taking) repricing a leg
    # faster than our 2s poll can cancel is the classic adverse-selection trap.
    # We watch three signals off the public trades tape + l2Book and either
    # widen the hit side, cancel the hit side, or pull both sides + stand down.
    # All-default OFF so existing behavior is byte-identical (opt-in).
    toxicity_enabled: bool = False
    # Trade-flow imbalance (TFI): signed buy/sell volume over a rolling window.
    tfi_window_s: float = 10.0  # time horizon of the tape window
    tfi_window_trades: int = 50  # cap the window to the last N trades
    tfi_min_tape_sz: float = 5.0  # min Σsize before TFI is trusted (else 0)
    tfi_min_trades: int = 3  # min trade count before TFI is trusted (else 0)
    tfi_cancel: float = 0.6  # |TFI| ≥ this → cancel/widen the hit side
    tfi_pull: float = 0.8  # |TFI| ≥ this → pull both sides + stand down
    # Queue imbalance (QI): (bid_sz - ask_sz)/(bid_sz + ask_sz) from the book.
    qi_confirm: float = 0.6  # QI past this in the TFI direction escalates a cancel→pull
    # Depth evaporation: a side whose top size drops > this fraction tick-to-tick.
    depth_evap_frac: float = 0.50
    # How many extra ticks to push the hit side away on a CANCEL-SIDE event.
    toxicity_widen_ticks: int = 3
    # How long to refuse to quote after a PULL (both-side stand-down).
    standdown_s: float = 45.0
    # Require a non-empty tape for any toxicity action (else QI + depth-evap
    # alone can act from the book). Default off → book-only signals still work.
    toxicity_require_tape: bool = False


@dataclass
class _OpenOrders:
    """Tracking for orders we currently have on the book."""

    bid_oid: int | None = None
    bid_px: float = 0.0
    ask_oid: int | None = None
    ask_px: float = 0.0
    last_quote_at: float = 0.0
    last_mid: float = 0.0
    fills: list[dict[str, Any]] = field(default_factory=list)


class OutcomeMaker:
    """Drives the maker strategy for a single outcome leg.

    Assumes `info` and `exchange.info` already have the outcome's asset ID
    registered (call `hl_outcome.register_outcome_assets()` before constructing).
    """

    def __init__(
        self,
        info: InfoProto,
        exchange: ExchangeProto,
        market_meta: MarketMeta,
        journal: Journal,
        config: MakerConfig,
        dry_run: bool = True,
        account_address: str = "",
    ):
        self.info = info
        self.exchange = exchange
        self.market_meta = market_meta
        self.journal = journal
        self.cfg = config
        self.dry_run = dry_run
        self.account_address = account_address
        self._lock = Lock()
        self._open = _OpenOrders()
        self._inventory_shares: float = 0.0
        self._inventory_cost: float = 0.0  # total $ paid to acquire current inventory
        self._seen_tids: set[int] = set()  # dedupe userFills across WS replays/snapshots
        # Rolling mid buffer for the volatility estimator (GLFT skew). Bounded
        # so memory is constant regardless of runtime.
        self._mids: deque[float] = deque(maxlen=max(2, config.sigma_window))
        self._last_sigma: float = config.sigma_base
        # Rolling baseline of the YES-probability `p` for the dark-horse
        # circuit-breaker. Bounded; reuses the sigma window length.
        self._p_hist: deque[float] = deque(maxlen=max(2, config.sigma_window))
        # Match-event gating: precompute (window_start, window_end) intervals
        # once at construction (file path wins over the inline list). Loaded
        # only when the gate is enabled — zero overhead otherwise.
        # `_match_gate_degraded` is set by `_load_match_windows` when an enabled
        # gate cannot be trusted (configured fixtures file failed to load with no
        # usable fallback, or no fixtures at all). A degraded gate FAILS CLOSED:
        # `_match_gate_active` returns True for all times so the maker refuses to
        # quote rather than quote unprotected against adverse selection.
        self._match_gate_degraded: bool = False
        self._match_windows: list[tuple[int, int]] = (
            self._load_match_windows() if config.match_gate_enabled else []
        )
        # Toxicity / flow-imbalance defense. The public trades tape feeds the
        # TFI signal; QI + depth-evaporation come off the l2Book we already
        # poll. WS runs off-thread → mutations of the tape/seen-set are guarded
        # by `self._lock`. `_seen_trade_tids` is SEPARATE from `_seen_tids`
        # (fills vs trades are different id spaces) and bounded so it can't grow
        # unbounded. `_last_book_top` is the prior tick's EXTERNAL (own-stripped)
        # top, used to detect depth evaporation. While `now < _standdown_until`
        # the maker refuses to quote (stand-down after a toxic pull).
        self._tape: deque[dict[str, Any]] = deque()  # time+count pruned on read
        self._seen_trade_tids: set[int] = set()
        self._last_book_top: dict[str, float] | None = None
        self._standdown_until: float = 0.0

    # ---------- public API ----------

    def run(self) -> None:
        """Main loop. Runs until expiry-buffer OR KILL switch OR exception."""
        log.info(
            "OutcomeMaker armed: coin=%s expiry=%s min_spread_bps=%s qty=%s "
            "max_pos=%s max_inv$=%s dry_run=%s",
            self.cfg.coin,
            self.cfg.expiry_ts,
            self.cfg.min_spread_bps,
            self.cfg.quote_size_shares,
            self.cfg.max_position_shares,
            self.cfg.max_inventory_usd,
            self.dry_run,
        )
        self.journal.write(
            "maker_start",
            coin=self.cfg.coin,
            expiry_ts=self.cfg.expiry_ts,
            min_spread_bps=self.cfg.min_spread_bps,
            dry_run=self.dry_run,
        )
        # Seed inventory from on-chain state BEFORE the first quote so risk caps
        # are computed against real exposure (not a from-flat 0.0). Without this,
        # a position left by a prior run / manual trade / a fill that landed
        # before the WS attached is invisible and the cap checks can be breached.
        self.reconcile_inventory_from_chain()
        try:
            while not self._should_stop():
                try:
                    self.tick()
                except Exception:
                    log.exception("maker tick error")
                    self.journal.write("maker_tick_error", coin=self.cfg.coin)
                time.sleep(self.cfg.refresh_interval_s)
        finally:
            self._cancel_all("shutdown")
            self.journal.write(
                "maker_stop",
                coin=self.cfg.coin,
                inventory_shares=self._inventory_shares,
                inventory_cost=self._inventory_cost,
            )

    def reconcile_inventory_from_chain(self) -> None:
        """Overwrite local inventory with HL's authoritative on-chain position.

        The maker's only other inventory source is the live userFills WS stream
        (handle_ws_fills -> on_own_fill). That stream cannot see a position that
        already existed before this process attached: a prior run, a manual
        trade, or a fill that landed before the WS subscription. Without this
        seed the maker believes it holds 0 and its caps (max_position_shares /
        max_inventory_usd) are checked against the wrong inventory, so it can
        post bids that breach the exposure cap.

        Mirrors `positions.reconcile_with_user_state` for a single outcome coin.
        HIP-4 outcome legs live in `spotClearinghouseState.balances` as `+NN`
        coins (NOT in `assetPositions`); perp coins live in `assetPositions`.
        We check both so this works for either surface. Long-only: clamp >= 0.

        Defensive: if the address is empty, the fetch fails, or the coin isn't
        found, we log and leave inventory at 0.0 (current behavior) — never
        crash startup.
        """
        if not self.account_address:
            return
        try:
            sz, cost = self._fetch_chain_inventory()
        except Exception:
            log.exception(
                "maker: reconcile_inventory_from_chain failed for %s — leaving at 0.0",
                self.cfg.coin,
            )
            self.journal.write(
                "maker_reconcile_failed", coin=self.cfg.coin
            )
            return
        sz = max(0.0, sz)
        cost = max(0.0, cost)
        with self._lock:
            self._inventory_shares = sz
            self._inventory_cost = cost
        log.info(
            "maker: reconciled inventory for %s: shares=%s cost=$%s",
            self.cfg.coin,
            sz,
            cost,
        )
        self.journal.write(
            "maker_reconcile",
            coin=self.cfg.coin,
            inventory_shares=sz,
            inventory_cost=cost,
        )

    def _fetch_chain_inventory(self) -> tuple[float, float]:
        """Return (shares, cost_basis_usd) for this maker's coin from HL state.

        Returns (0.0, 0.0) when the coin isn't held. Raises on a hard fetch
        failure (caller treats that as "leave at 0.0").
        """
        # Perp / futures surface: assetPositions keyed by the trade coin (`#NN`).
        us = self.info.user_state(self.account_address) or {}
        for ap in us.get("assetPositions", []) or []:
            pos = ap.get("position") if isinstance(ap, dict) else None
            if not isinstance(pos, dict):
                continue
            if pos.get("coin") != self.cfg.coin:
                continue
            try:
                szi = float(pos.get("szi", 0) or 0)
                entry_px = float(pos.get("entryPx", 0) or 0)
            except (TypeError, ValueError):
                log.warning("maker: malformed position %s", pos)
                return (0.0, 0.0)
            return (szi, szi * entry_px)

        # HIP-4 outcome surface: spotClearinghouseState.balances as `+NN`.
        # Translate our trade coin `#NN` -> spot ticker `+NN`.
        spot_coin = "+" + self.cfg.coin[1:] if self.cfg.coin.startswith("#") else None
        if spot_coin is not None:
            sc = (
                self.info.post(
                    "/info",
                    {"type": "spotClearinghouseState", "user": self.account_address},
                )
                or {}
            )
            for b in sc.get("balances", []) or []:
                if not isinstance(b, dict) or b.get("coin") != spot_coin:
                    continue
                try:
                    total = float(b.get("total", 0) or 0)
                    entry_ntl = float(b.get("entryNtl", 0) or 0)
                except (TypeError, ValueError):
                    log.warning("maker: malformed spot balance %s", b)
                    return (0.0, 0.0)
                if total <= 0:
                    return (0.0, 0.0)
                return (total, entry_ntl)

        return (0.0, 0.0)

    def tick(self) -> None:
        """Single quote-cycle. Public so tests can drive it directly."""
        # Stand-down gate (toxicity). After a toxic PULL we refuse to quote for
        # `standdown_s`: cancel everything and bail BEFORE the match-gate / book
        # fetch. Opt-in: skipped entirely when toxicity is disabled so existing
        # behavior is byte-identical.
        if self.cfg.toxicity_enabled and time.time() < self._standdown_until:
            self._cancel_all("standdown")
            self.journal.write("maker_skip", coin=self.cfg.coin, reason="standdown")
            return
        # Match-event gate. If a configured fixture is live (preroll → cooldown),
        # cancel everything first then bail BEFORE touching the book — entering a
        # window pulls all resting quotes and we refuse to post adverse-selected
        # quotes against a repricing WINNER leg.
        if self._match_gate_active(time.time()):
            self._cancel_all("match_gate")
            self.journal.write("maker_skip", coin=self.cfg.coin, reason="match_gate")
            return
        book = self._fetch_book()
        if book is None:
            return
        bids, asks = book["levels"][0], book["levels"][1]
        # Remove our own resting orders from the book BEFORE deriving best
        # bid/ask. l2Book includes our own quotes; if we don't strip them we'd
        # read our own ask back as best_ask and undercut ourselves every tick —
        # a monotonic self-referential walk away from the true market.
        bids = self._strip_own_level(bids, self._open.bid_oid, self._open.bid_px)
        asks = self._strip_own_level(asks, self._open.ask_oid, self._open.ask_px)
        if not bids or not asks:
            self.journal.write("maker_skip", coin=self.cfg.coin, reason="empty_book")
            return
        try:
            best_bid = float(bids[0]["px"])
            best_ask = float(asks[0]["px"])
        except (KeyError, TypeError, ValueError):
            self.journal.write("maker_skip", coin=self.cfg.coin, reason="malformed_book")
            return
        mid = (best_bid + best_ask) / 2
        # Feed the rolling buffer and refresh the volatility estimate BEFORE
        # the spread gate so sigma keeps tracking even on ticks we skip.
        self._mids.append(mid)
        sigma_eff = self._estimate_sigma(mid)
        # Feed the NO-skew probability baseline (used by the dark-horse
        # circuit-breaker). Track even on skipped ticks so the baseline is
        # current. The breaker compares the just-computed `p` against the
        # rolling min INCLUDING this sample, which is fine: a single doubled
        # sample still trips it (its own value isn't the min once a lower prior
        # exists) and the very first sample can't false-trip (it IS the min).
        if self.cfg.noskew_enabled and 0.0 < mid < 1.0:
            self._p_hist.append(self._yes_prob(mid))
        spread = best_ask - best_bid
        spread_bps = (spread / mid) * 10_000 if mid > 0 else 0
        if spread_bps < self.cfg.min_spread_bps:
            self.journal.write(
                "maker_skip",
                coin=self.cfg.coin,
                reason="spread_too_tight",
                spread_bps=spread_bps,
                floor_bps=self.cfg.min_spread_bps,
            )
            self._cancel_all("spread_too_tight")
            return

        # External (own-stripped) top-of-book sizes — used by QI + depth-evap.
        try:
            bid_sz = float(bids[0].get("sz", 0.0))
            ask_sz = float(asks[0].get("sz", 0.0))
        except (TypeError, ValueError):
            bid_sz = ask_sz = 0.0
        cur_top = {"bid_px": best_bid, "bid_sz": bid_sz, "ask_px": best_ask, "ask_sz": ask_sz}

        # Toxicity / flow-imbalance defense. Opt-in: skipped entirely when
        # disabled (no signals computed, no widen) so behavior is byte-identical.
        widen_bid = 0
        widen_ask = 0
        if self.cfg.toxicity_enabled:
            now = time.time()
            tfi, pressure = self._compute_tfi(now)
            qi = self._compute_qi(bid_sz, ask_sz)
            bid_evap, ask_evap = self._compute_depth_evap(self._last_book_top, cur_top)
            atfi = abs(tfi)
            have_tape = pressure is not None
            tape_ok = have_tape or not self.cfg.toxicity_require_tape
            # "Hit side" = the side takers are consuming. Buy pressure (TFI>0,
            # takers lifting asks) hits the ASK; sell pressure hits the BID.
            hit_is_ask = tfi > 0
            # QI confirms the TFI direction when it leans the same way past the
            # confirm threshold: buy pressure wants bid-heavy book (QI>0), sell
            # pressure wants ask-heavy (QI<0).
            qi_confirms = have_tape and (
                (tfi > 0 and qi >= self.cfg.qi_confirm)
                or (tfi < 0 and qi <= -self.cfg.qi_confirm)
            )
            both_evap = bid_evap and ask_evap
            pull = (
                (tape_ok and atfi >= self.cfg.tfi_pull)
                or (tape_ok and atfi >= self.cfg.tfi_cancel and qi_confirms)
                or both_evap
            )
            one_evap = bid_evap or ask_evap
            cancel_side = (tape_ok and atfi >= self.cfg.tfi_cancel) or one_evap

            if pull:
                self._standdown_until = now + self.cfg.standdown_s
                self._cancel_all("toxicity_pull")
                self.journal.write(
                    "maker_toxicity",
                    coin=self.cfg.coin,
                    action="pull",
                    tfi=tfi,
                    qi=qi,
                    bid_evap=bid_evap,
                    ask_evap=ask_evap,
                    standdown_until=self._standdown_until,
                )
                self._last_book_top = cur_top
                return
            if cancel_side:
                # Determine the hit side. With a trustworthy TFI use its
                # direction; otherwise (book-only / evap) use the evaporated side.
                if have_tape and atfi >= self.cfg.tfi_cancel:
                    hit = "A" if hit_is_ask else "B"
                elif ask_evap and not bid_evap:
                    hit = "A"
                elif bid_evap and not ask_evap:
                    hit = "B"
                else:
                    hit = "A" if hit_is_ask else "B"
                if hit == "A":
                    if self._open.ask_oid is not None:
                        self._cancel_one("ask", "toxicity_cancel_side")
                    widen_ask = self.cfg.toxicity_widen_ticks
                else:
                    if self._open.bid_oid is not None:
                        self._cancel_one("bid", "toxicity_cancel_side")
                    widen_bid = self.cfg.toxicity_widen_ticks
                self.journal.write(
                    "maker_toxicity",
                    coin=self.cfg.coin,
                    action="cancel_side",
                    hit=hit,
                    tfi=tfi,
                    qi=qi,
                    bid_evap=bid_evap,
                    ask_evap=ask_evap,
                    widen_ticks=self.cfg.toxicity_widen_ticks,
                )

        bid_px, ask_px = self._compute_quotes(
            mid, best_bid, best_ask, sigma_eff, widen_bid=widen_bid, widen_ask=widen_ask
        )
        if bid_px is None and ask_px is None:
            # Both sides suppressed (caps, sanity, or crossed) — cancel anything
            # resting and wait for next tick.
            self._cancel_all("no_quote_target")
            if self.cfg.toxicity_enabled:
                self._last_book_top = cur_top
            return
        self._reconcile(bid_px, ask_px, mid)
        # Record the external top for next tick's depth-evaporation comparison.
        if self.cfg.toxicity_enabled:
            self._last_book_top = cur_top

    def on_own_fill(self, fill: dict[str, Any]) -> None:
        """Called by external WS subscriber when our order fills.

        Updates inventory + cost basis. Does NOT immediately re-quote — that
        happens on the next tick().
        """
        try:
            sz = float(fill.get("sz", 0))
            px = float(fill.get("px", 0))
            side = fill.get("side")
        except (TypeError, ValueError):
            return

        # HIP-4 binary outcomes settle to 0 (loser) or 1 (winner) at expiry. HL
        # emits a fill with dir="Settlement"; the losing side carries px=0, which
        # the generic guard below would otherwise drop, leaving phantom inventory
        # forever. A settlement of either side resolves the position, so flatten.
        if fill.get("dir") == "Settlement" and sz > 0:
            with self._lock:
                self._inventory_shares = 0.0
                self._inventory_cost = 0.0
                self._open.fills.append(dict(fill))
            self.journal.write(
                "maker_fill",
                coin=self.cfg.coin,
                side=side,
                sz=sz,
                px=px,
                settlement=True,
                inventory_shares=self._inventory_shares,
                inventory_cost=self._inventory_cost,
            )
            return

        if sz <= 0 or px <= 0 or side not in ("B", "A"):
            return
        with self._lock:
            if side == "B":
                self._inventory_shares += sz
                self._inventory_cost += sz * px
            else:
                # Selling: realize PnL on shares we held. Long-only accounting —
                # clamp so inventory never goes negative (an oversell can at most
                # flatten us), which would otherwise corrupt the average cost.
                avg = (
                    self._inventory_cost / self._inventory_shares
                    if self._inventory_shares > 0
                    else 0.0
                )
                self._inventory_shares = max(0.0, self._inventory_shares - sz)
                self._inventory_cost = max(0.0, self._inventory_shares * avg)
            self._open.fills.append(dict(fill))
        self.journal.write(
            "maker_fill",
            coin=self.cfg.coin,
            side=side,
            sz=sz,
            px=px,
            inventory_shares=self._inventory_shares,
            inventory_cost=self._inventory_cost,
        )

    def handle_ws_fills(self, msg: Any) -> None:
        """Route a `userFills` WS message to `on_own_fill`.

        This is the wiring that was missing: the standalone entrypoint now
        subscribes to `{"type": "userFills"}` and points it here. Without it,
        live fills never reached `on_own_fill`, so `_inventory_shares` stayed
        0.0 and the maker would keep posting bids past its position cap, blind.

        Responsibilities (mirrors `follower._handle` semantics):
          - unwrap the `{"data": {"fills": [...], "isSnapshot": bool}}` envelope
          - keep only fills for THIS maker's coin (userFills spans all markets)
          - dedupe by `tid` so a WS reconnect/replay can't double-count
          - skip the initial `isSnapshot` batch — those are historical fills
            that predate this process; applying them would corrupt the
            from-flat inventory the maker rebuilds at startup
        """
        data = msg.get("data", msg) if isinstance(msg, dict) else {}
        fills = data.get("fills", []) or []
        is_snapshot = bool(data.get("isSnapshot", False))
        for f in fills:
            if not isinstance(f, dict):
                continue
            if f.get("coin") != self.cfg.coin:
                continue  # not our market
            tid = f.get("tid")
            if tid is not None:
                try:
                    tid_int = int(tid)
                except (TypeError, ValueError):
                    tid_int = None
                if tid_int is not None:
                    if tid_int in self._seen_tids:
                        continue
                    self._seen_tids.add(tid_int)
            if is_snapshot:
                continue  # historical — mark seen above, but don't apply to inventory
            self.on_own_fill(f)

    def handle_ws_trades(self, msg: Any) -> None:
        """Route a public `trades` WS message onto the rolling tape.

        The `trades` stream is the PUBLIC tape (all participants), not our own
        fills — it feeds the trade-flow-imbalance (TFI) toxicity signal. Shape:
        `{"data": [ {coin, side, sz, px, tid, time}, ... ]}` where the payload
        is a LIST of trades. `side` is the aggressor: "B" = buy aggressor
        (bullish, takers lifting asks), "A" = sell aggressor (bearish).

        Responsibilities:
          - unwrap the `{"data": [...]}` envelope (payload is a list)
          - keep only trades for THIS maker's coin (the feed spans markets)
          - dedupe by `tid` via `_seen_trade_tids` (SEPARATE from the fills
            `_seen_tids`) so a WS reconnect/replay can't double-count; bound the
            set so it can't grow unbounded over a long run
          - drop malformed prints (non-numeric sz/px/time, bad side)
          - append `{ts, side, sz, px}` to the tape

        Thread-safe: the WS runs off-thread, so tape + seen-set mutations are
        guarded by `self._lock`.
        """
        data = msg.get("data", msg) if isinstance(msg, dict) else []
        if not isinstance(data, list):
            return
        for t in data:
            if not isinstance(t, dict):
                continue
            if t.get("coin") != self.cfg.coin:
                continue  # not our market
            tid = t.get("tid")
            tid_int: int | None = None
            if tid is not None:
                try:
                    tid_int = int(tid)
                except (TypeError, ValueError):
                    tid_int = None
            side = t.get("side")
            if side not in ("B", "A"):
                continue
            try:
                sz = float(t.get("sz"))
                px = float(t.get("px"))
                ts = float(t.get("time")) / 1000.0
            except (TypeError, ValueError):
                continue
            if not (math.isfinite(sz) and math.isfinite(px) and math.isfinite(ts)):
                continue
            with self._lock:
                if tid_int is not None:
                    if tid_int in self._seen_trade_tids:
                        continue
                    self._seen_trade_tids.add(tid_int)
                    # Bound the dedupe set so it can't grow without limit over a
                    # long run. Keep it generously larger than any plausible
                    # tape window; prune oldest-ish by clearing the excess.
                    if len(self._seen_trade_tids) > 4096:
                        # Drop an arbitrary half — re-seeing a very old tid is
                        # harmless (it'd just re-append a long-pruned trade,
                        # which the read-side window prune discards anyway).
                        for old in list(self._seen_trade_tids)[:2048]:
                            self._seen_trade_tids.discard(old)
                self._tape.append({"ts": ts, "side": side, "sz": sz, "px": px})

    # ---------- internals ----------

    def _compute_tfi(self, now: float) -> tuple[float, str | None]:
        """Trade-flow imbalance over the rolling tape window.

        Prunes the tape to the last `tfi_window_s` seconds AND the last
        `tfi_window_trades` prints (whichever is tighter), then computes the
        signed volume imbalance:

            TFI = (buy_vol - sell_vol) / (buy_vol + sell_vol)  ∈ [-1, 1]

        Returns (0.0, None) — inert — unless there is enough flow to trust:
        Σsize ≥ `tfi_min_tape_sz` AND count ≥ `tfi_min_trades`. An empty tape
        therefore yields (0.0, None), so TFI never acts on no data.

        pressure = "B" (buy/bullish) when TFI > 0, "A" (sell/bearish) when < 0.
        """
        with self._lock:
            cutoff = now - self.cfg.tfi_window_s
            while self._tape and self._tape[0]["ts"] < cutoff:
                self._tape.popleft()
            while len(self._tape) > self.cfg.tfi_window_trades:
                self._tape.popleft()
            trades = list(self._tape)
        if not trades:
            return (0.0, None)
        buy = sum(t["sz"] for t in trades if t["side"] == "B")
        sell = sum(t["sz"] for t in trades if t["side"] == "A")
        total = buy + sell
        if total < self.cfg.tfi_min_tape_sz or len(trades) < self.cfg.tfi_min_trades:
            return (0.0, None)
        if total <= 0:
            return (0.0, None)
        tfi = (buy - sell) / total
        return (tfi, "B" if tfi > 0 else "A")

    @staticmethod
    def _compute_qi(bid_sz: float, ask_sz: float) -> float:
        """Queue imbalance from the (external) top-of-book sizes.

            QI = (bid_sz - ask_sz) / (bid_sz + ask_sz)  ∈ [-1, 1]

        Positive = bid-heavy (buy pressure), negative = ask-heavy. 0 on a
        degenerate/empty book. Sizes must be the OWN-STRIPPED external sizes.
        """
        denom = bid_sz + ask_sz
        if denom <= 0:
            return 0.0
        return (bid_sz - ask_sz) / denom

    def _compute_depth_evap(
        self, last_top: dict[str, float] | None, cur_top: dict[str, float]
    ) -> tuple[bool, bool]:
        """Detect top-of-book depth evaporation per side, tick-to-tick.

        For each side `drop = (last_sz - cur_sz) / last_sz`; the side is flagged
        when `drop > depth_evap_frac` (default 0.50 = half the size pulled). On
        a cold start (`last_top is None`) returns (False, False).

        Returns (bid_evaporated, ask_evaporated) using EXTERNAL sizes.
        """
        if last_top is None:
            return (False, False)
        frac = self.cfg.depth_evap_frac

        def _dropped(last_sz: float, cur_sz: float) -> bool:
            if last_sz <= 0:
                return False
            return (last_sz - cur_sz) / last_sz > frac

        bid_evap = _dropped(last_top.get("bid_sz", 0.0), cur_top.get("bid_sz", 0.0))
        ask_evap = _dropped(last_top.get("ask_sz", 0.0), cur_top.get("ask_sz", 0.0))
        return (bid_evap, ask_evap)

    def _load_match_windows(self) -> list[tuple[int, int]]:
        """Build (window_start, window_end) intervals from the fixture calendar.

        Each window is (kickoff_ts - preroll_s, est_end_ts + cooldown_s). The
        file path wins over the inline list when set. Malformed fixtures
        (missing / non-int timestamps) are skipped and journalled — loading
        never crashes startup.

        Fail-CLOSED safety contract (the gate exists to avoid adverse selection,
        so any uncertainty about its windows must NOT silently disable it):
          * file loads fine                         → normal windows, healthy.
          * file set, load fails, inline non-empty  → real fallback to inline.
          * file set, load fails, inline empty       → DEGRADED (fail closed).
          * no file, inline non-empty                → inline windows, healthy.
          * no file, inline empty                    → enabled-but-no-fixtures,
            almost certainly a misconfig → DEGRADED (fail closed). A safety gate
            with zero windows would otherwise quote unprotected forever.
        When degraded we set `self._match_gate_degraded = True` and loudly signal
        it (log.error + a distinct `match_gate_degraded_failclosed` journal event)
        so the operator can tell it from a normal quiet period; `_match_gate_active`
        then gates ALL times.
        """
        fixtures: list[Any] = []
        path = self.cfg.match_gate_fixtures_file
        inline = list(self.cfg.match_gate_fixtures)
        file_load_failed = False
        if path:
            try:
                with open(path) as f:
                    fixtures = json.load(f)
            except Exception as e:
                file_load_failed = True
                log.warning("maker: match_gate fixtures file unreadable %s: %s", path, e)
                self.journal.write(
                    "match_gate_file_unreadable", coin=self.cfg.coin, path=path, error=str(e)
                )
                # Inline list is a legitimate fallback ONLY if it actually has
                # fixtures; an empty inline list is handled by the fail-closed
                # check below rather than silently disabling the gate.
                fixtures = inline
        else:
            fixtures = inline

        # Fail closed: an enabled gate that ends up with no usable fixtures must
        # refuse to quote, not wave everything through.
        if (file_load_failed and not inline) or (not path and not inline):
            self._match_gate_degraded = True
            reason = "file_unreadable_no_inline_fallback" if file_load_failed else "no_fixtures_configured"
            log.error(
                "maker: match_gate DEGRADED (%s) — failing CLOSED, refusing to quote "
                "coin=%s path=%r",
                reason,
                self.cfg.coin,
                path,
            )
            self.journal.write(
                "match_gate_degraded_failclosed",
                coin=self.cfg.coin,
                reason=reason,
                path=path,
            )
            return []

        windows: list[tuple[int, int]] = []
        for fx in fixtures if isinstance(fixtures, list) else []:
            try:
                kickoff = int(fx["kickoff_ts"])
                est_end = int(fx["est_end_ts"])
            except (KeyError, TypeError, ValueError):
                self.journal.write(
                    "match_gate_bad_fixture", coin=self.cfg.coin, fixture=str(fx)
                )
                continue
            windows.append(
                (
                    kickoff - self.cfg.match_gate_preroll_s,
                    est_end + self.cfg.match_gate_cooldown_s,
                )
            )
        return windows

    def _match_gate_active(self, now: float) -> bool:
        """True if `now` falls inside any precomputed fixture window.

        Disabled gate → always False with zero overhead (no windows loaded).
        Degraded gate (enabled but fixtures couldn't be trusted) → always True:
        we fail CLOSED and gate every time so the maker refuses to quote.
        """
        if not self.cfg.match_gate_enabled:
            return False
        if self._match_gate_degraded:
            return True
        return any(start <= now <= end for start, end in self._match_windows)

    def _should_stop(self) -> bool:
        if os.path.exists(self.cfg.kill_switch_file):
            log.info("maker: KILL switch active — exiting")
            return True
        if time.time() >= self.cfg.expiry_ts - self.cfg.expiry_buffer_s:
            log.info("maker: within expiry buffer — exiting")
            return True
        return False

    def _fetch_book(self) -> dict[str, Any] | None:
        try:
            result = self.info.post("/info", {"type": "l2Book", "coin": self.cfg.coin})
        except Exception:
            log.exception("maker: l2Book fetch failed for %s", self.cfg.coin)
            return None
        return result if isinstance(result, dict) else None

    def _strip_own_level(
        self, levels: list[Any], own_oid: int | None, own_px: float
    ) -> list[Any]:
        """Remove our own resting size from `levels` so quote math sees only the
        EXTERNAL market.

        l2Book reflects the whole book, including our own resting quote. If we
        leave our order in, the best bid/ask we read back can be our OWN order,
        and we'd repeatedly undercut ourselves toward zero (HIGH-severity
        self-referential price walk) instead of pricing off the real market.

        We match "our" level by price equality at `own_px` (within half a tick).
        Approach: subtract our quote_size from the matching level; drop the level
        only if that empties it (i.e. we were the sole resters at that price).
        Subtracting (rather than always dropping the whole level) keeps any
        external size at the same price visible — so we still anchor to the true
        market when others rest alongside us.

        Tracking is keyed on a *set* oid/px: when own_oid is None we hold nothing
        on that side and the book is returned unchanged. In dry-run own_oid is -1
        (a fake oid) but no real order is on the book; stripping by price is then
        harmless — no level will match a phantom order's price unless the book
        coincidentally sits there, and even then anchoring to external size is
        the safe behavior.
        """
        if own_oid is None or own_px <= 0 or not levels:
            return levels
        # Half a tick of tolerance for float/round_price wobble.
        tol = (10**-5) / 2
        out: list[Any] = []
        for lvl in levels:
            try:
                lvl_px = float(lvl["px"])
                lvl_sz = float(lvl.get("sz", 0))
            except (KeyError, TypeError, ValueError):
                out.append(lvl)
                continue
            if abs(lvl_px - own_px) <= tol:
                remaining = lvl_sz - self.cfg.quote_size_shares
                if remaining > tol:
                    # External traders also rest here — keep their residual size.
                    out.append({**lvl, "sz": str(remaining)})
                # else: level was entirely (or essentially) ours → drop it.
                continue
            out.append(lvl)
        return out

    def _estimate_sigma(self, mid: float) -> float:
        """Estimate per-tick volatility (sigma_eff) for the GLFT skew.

        The outcome price is bounded in [0,1], so log-returns are wrong (they
        explode near 0). We use ARITHMETIC returns Δp_i = mid_i - mid_{i-1} and
        take their stdev over the rolling window. Before ~10 samples have
        accumulated we fall back to the configured `sigma_base`.

        We then apply a Bernoulli variance adjustment:

            sigma_eff = sigma_raw * sqrt(mid*(1-mid)) / 0.5

        This normalizes so sigma_eff == sigma_raw at mid=0.5 (where binary
        outcomes carry max variance) and collapses toward the 0/1 boundaries
        where the price is pinned. Mirrors the polymarket-mm probFactor logic
        (maker.go:301-302) using the exact Bernoulli sd sqrt(p(1-p)).

        Result is stored on `self._last_sigma` and always finite/non-negative.
        """
        mids = list(self._mids)
        # Arithmetic per-tick returns; need ≥10 diffs (≥11 mids) for a stable
        # stdev — below that, trust the configured base.
        if len(mids) >= 11:
            diffs = [mids[i] - mids[i - 1] for i in range(1, len(mids))]
            try:
                sigma_raw = statistics.stdev(diffs)
            except statistics.StatisticsError:
                sigma_raw = self.cfg.sigma_base
        else:
            sigma_raw = self.cfg.sigma_base
        if not math.isfinite(sigma_raw) or sigma_raw < 0:
            sigma_raw = self.cfg.sigma_base

        # Bernoulli adjustment. Guard mid strictly inside (0,1); at the
        # boundaries the variance is 0 and the price is pinned, so sigma → 0.
        if 0.0 < mid < 1.0:
            sigma_eff = sigma_raw * math.sqrt(mid * (1.0 - mid)) / 0.5
        else:
            sigma_eff = 0.0
        sigma_eff = max(0.0, sigma_eff)
        self._last_sigma = sigma_eff
        return sigma_eff

    def _yes_prob(self, mid: float) -> float:
        """YES-probability `p` implied by this leg's mid.

        The coin name is "#NN" where NN = 10*outcome_id + side (0=YES, 1=NO).
        For a YES leg the mid already IS the YES probability; for a NO leg the
        mid is the NO probability so the YES probability is 1 - mid. Non-numeric
        coin names are treated as YES (the safe, identity branch).
        """
        try:
            side = int(self.cfg.coin.lstrip("#")) % 10
        except (TypeError, ValueError):
            side = 0
        return (1.0 - mid) if side == 1 else mid

    def _noskew_bps(self, p: float) -> float:
        """Price-graded NO-skew magnitude in bps of fair-value shift.

        Continuous piecewise-linear in the YES-probability `p`:

          - p < p_floor or p > p_cap  → 0  (noise floor / favorite reversal)
          - [p_floor, knee_p]         → bps_at_floor → bps_at_knee   (steep)
          - (knee_p, p_cap]           → bps_at_knee  → 0             (shallow)

        Anchor points are config params (defaults: 250 bps @ 0.02, 100 bps @
        the 0.10 knee, 0 bps @ 0.50). Guards degenerate/empty bands → 0.
        """
        cfg = self.cfg
        floor, knee, cap = cfg.noskew_p_floor, cfg.noskew_knee_p, cfg.noskew_p_cap
        if not (floor <= p <= cap):
            return 0.0
        b_floor, b_knee = cfg.noskew_bps_at_floor, cfg.noskew_bps_at_knee
        if p <= knee:
            span = knee - floor
            if span <= 0:
                return b_knee
            frac = (p - floor) / span
            return b_floor + frac * (b_knee - b_floor)
        span = cap - knee
        if span <= 0:
            return 0.0
        frac = (p - knee) / span
        return b_knee + frac * (0.0 - b_knee)

    def _noskew_shift(self, mid: float) -> float:
        """Coin-price-space fair-value shift from the NO-skew edge.

        Positive = push the quote pair UP (buy more eagerly), negative = down.
        The edge de-biases YES *down*: a YES leg gets a downward push, a NO leg
        an upward push (so we buy cheap NO more eagerly). Returns 0 when the
        edge is disabled, off-band, mid is degenerate, or the dark-horse
        circuit-breaker fires.
        """
        if not self.cfg.noskew_enabled:
            return 0.0
        if not (0.0 < mid < 1.0):
            return 0.0
        p = self._yes_prob(mid)
        # Dark-horse circuit-breaker: if p has surged to >= surge_mult * the
        # recent rolling minimum, the longshot is moving against us — neutralize
        # the lean rather than fade a surging dark horse.
        if self._p_hist:
            base = min(self._p_hist)
            if base > 0 and p >= self.cfg.noskew_surge_mult * base:
                self.journal.write(
                    "noskew_circuit_breaker",
                    coin=self.cfg.coin,
                    p=p,
                    baseline_p=base,
                    surge_mult=self.cfg.noskew_surge_mult,
                )
                return 0.0
        bps = self._noskew_bps(p)
        if bps <= 0:
            return 0.0
        frac = (bps / 10_000.0) * mid
        # YES leg → push YES fair value DOWN; NO leg → push UP (buy NO eagerly).
        try:
            side = int(self.cfg.coin.lstrip("#")) % 10
        except (TypeError, ValueError):
            side = 0
        return frac if side == 1 else -frac

    def _glft_c2(self) -> float:
        """GLFT inventory-skew coefficient c2.

            c2 = sqrt( γ / (2 A k) * (1 + γ/k) ** (k/γ + 1) )

        Ported from the Guéant-Lehalle-Fernandez-Tapia closed form (the
        polymarket-mm reservation-price skew, maker.go:308-314, uses the same
        γ/k group). Numerically guarded: non-positive A/k/γ collapse the skew
        to 0 rather than producing NaN/inf.
        """
        gamma = self.cfg.gamma
        k = self.cfg.glft_k
        a = self.cfg.glft_A
        if gamma <= 0 or k <= 0 or a <= 0:
            return 0.0
        inner = gamma / (2.0 * a * k) * (1.0 + gamma / k) ** (k / gamma + 1.0)
        if not math.isfinite(inner) or inner <= 0:
            return 0.0
        return math.sqrt(inner)

    def _compute_quotes(
        self,
        mid: float,
        best_bid: float,
        best_ask: float,
        sigma_eff: float = 0.0,
        widen_bid: int = 0,
        widen_ask: int = 0,
    ) -> tuple[float | None, float | None]:
        """Return (bid_px, ask_px) to quote, or None on either side to abstain.

        Order of checks (each side independently):
          1. Compute target prices with inventory skew.
          2. Round to HL ticks; refuse if quotes would cross.
          3. Apply inventory caps (suppresses a side independently).
          4. Apply sanity bounds (only against still-active sides).

        `widen_bid` / `widen_ask` are extra offset TICKS pushed onto that side
        AWAY from the mid (toxicity CANCEL-SIDE widen). They compose ADDITIVELY
        with the `quote_offset_ticks` base, the GLFT inventory skew, and the
        NO-skew fair-value shift — none of which are touched.
        """
        # Normalized inventory q ∈ [0,1] (long-only ⇒ q ≥ 0).
        q = self._inventory_shares / max(1.0, self.cfg.max_position_shares)
        if self.cfg.use_glft_skew:
            # Vol-aware GLFT skew: walk the whole quote pair DOWN by
            # `q * sigma_eff * c2`. Larger inventory or higher volatility ⇒
            # more aggressive lean toward selling.
            skew = q * sigma_eff * self._glft_c2()
        else:
            # Legacy crude linear skew (backward-compat / pinnable in tests).
            skew = (q * self.cfg.inventory_skew_bps_at_full / 10_000.0) * mid
        # Favorite-longshot NO-skew: a fair-value shift applied to BOTH sides
        # (shifts the whole pair), composed with the GLFT inventory skew.
        # Directional offset of the pair = (-skew + fv_shift): GLFT walks down,
        # NO-skew pushes per its sign. Clamp the COMBINED offset to
        # combined_skew_cap_frac * mid so neither lean can walk a quote across
        # the book. Scale BOTH contributions proportionally when they exceed it.
        fv_shift = self._noskew_shift(mid)
        if self.cfg.noskew_enabled:
            # Clamp the COMBINED offset only when the edge is live. When the
            # edge is OFF, fv_shift is 0 and we leave the pre-existing GLFT skew
            # path entirely untouched (byte-identical), even if that skew alone
            # would exceed the cap — the cap exists to bound the *combined* lean.
            skew_total = -skew + fv_shift
            cap = self.cfg.combined_skew_cap_frac * mid
            if cap > 0 and abs(skew_total) > cap:
                scale = cap / abs(skew_total)
                skew *= scale
                fv_shift *= scale
        proposed_bid = (
            best_bid + (10**-5) * (self.cfg.quote_offset_ticks + widen_bid) - skew + fv_shift
        )
        proposed_ask = (
            best_ask - (10**-5) * (self.cfg.quote_offset_ticks + widen_ask) - skew + fv_shift
        )
        bid_px = self.market_meta.round_price(proposed_bid)
        ask_px = self.market_meta.round_price(proposed_ask)

        if bid_px >= ask_px:
            self.journal.write(
                "maker_skip",
                coin=self.cfg.coin,
                reason="quotes_crossed",
                bid=bid_px,
                ask=ask_px,
            )
            return (None, None)

        bid_active = True
        ask_active = True

        # Don't grow long past position cap
        if (self._inventory_shares + self.cfg.quote_size_shares > self.cfg.max_position_shares) or (
            self._inventory_cost + bid_px * self.cfg.quote_size_shares > self.cfg.max_inventory_usd
        ):
            bid_active = False
        # Don't sell what we don't have (no shorts)
        if self._inventory_shares < self.cfg.quote_size_shares:
            ask_active = False

        # Sanity bounds — reject only the side that's outside; the other can quote.
        if bid_active and not (self.cfg.min_quote_px <= bid_px <= self.cfg.max_quote_px):
            self.journal.write(
                "maker_skip",
                coin=self.cfg.coin,
                reason="bid_out_of_bounds",
                bid=bid_px,
            )
            bid_active = False
        if ask_active and not (self.cfg.min_quote_px <= ask_px <= self.cfg.max_quote_px):
            self.journal.write(
                "maker_skip",
                coin=self.cfg.coin,
                reason="ask_out_of_bounds",
                ask=ask_px,
            )
            ask_active = False

        return (bid_px if bid_active else None, ask_px if ask_active else None)

    def _reconcile(self, bid_px: float | None, ask_px: float | None, mid: float) -> None:
        """Cancel + replace orders based on new desired quotes."""
        now = time.time()
        # Throttle: don't churn faster than refresh_interval
        if now - self._open.last_quote_at < self.cfg.refresh_interval_s:
            return
        # If mid moved less than threshold, don't bother replacing
        moved_bps = (
            abs(mid - self._open.last_mid) / max(self._open.last_mid, 1e-9) * 10_000
            if self._open.last_mid > 0
            else float("inf")
        )
        if moved_bps < self.cfg.cancel_threshold_bps and self._open.bid_oid:
            return

        # Cancel anything resting
        self._cancel_all("reposting")

        # If a side still has a tracked oid, its cancel failed: the old order
        # is still resting. Skip placing a new order on that side this cycle to
        # avoid doubling exposure; the next _cancel_all will retry the cancel.
        if bid_px is not None and self._open.bid_oid is None:
            self._place(side="B", px=bid_px)
        if ask_px is not None and self._open.ask_oid is None:
            self._place(side="A", px=ask_px)

        self._open.last_mid = mid
        self._open.last_quote_at = now

    def _place(self, side: str, px: float) -> None:
        is_buy = side == "B"
        sz = self.cfg.quote_size_shares
        sz_rounded = self.market_meta.round_size(self.cfg.coin, sz)
        if sz_rounded <= 0:
            self.journal.write("maker_skip", coin=self.cfg.coin, reason="zero_sz_after_round")
            return
        if self.dry_run:
            log.info("[DRY] maker %s %s sz=%s @ %.6f", side, self.cfg.coin, sz_rounded, px)
            self.journal.write(
                "maker_quote_dry",
                coin=self.cfg.coin,
                side=side,
                sz=sz_rounded,
                px=px,
            )
            # Track a fake oid so reconcile sees us as "have orders"
            if side == "B":
                self._open.bid_oid = -1
                self._open.bid_px = px
            else:
                self._open.ask_oid = -1
                self._open.ask_px = px
            return
        try:
            result = self.exchange.order(
                self.cfg.coin,
                is_buy,
                sz_rounded,
                px,
                order_type={"limit": {"tif": "Alo"}},  # post-only
                reduce_only=False,
            )
        except Exception as e:
            self.journal.write(
                "maker_quote_failed",
                coin=self.cfg.coin,
                side=side,
                px=px,
                error=str(e),
            )
            log.exception("maker order submit failed")
            raise OrderError(f"maker order failed: {e}") from e
        oid = self._extract_oid(result)
        if oid is not None:
            if side == "B":
                self._open.bid_oid = oid
                self._open.bid_px = px
            else:
                self._open.ask_oid = oid
                self._open.ask_px = px
        self.journal.write(
            "maker_quote",
            coin=self.cfg.coin,
            side=side,
            sz=sz_rounded,
            px=px,
            oid=oid,
        )

    def _cancel_one(self, side: str, reason: str) -> None:
        """Cancel one resting side ("bid" or "ask"). Same semantics as
        `_cancel_all` but scoped to a single side: only clears the local
        oid/px AFTER a successful cancel; on cancel failure the oid is kept so
        a later `_cancel_all`/`_cancel_one` retries and the order is never
        orphaned. Used by the toxicity CANCEL-SIDE action.
        """
        oid_attr = "bid_oid" if side == "bid" else "ask_oid"
        px_attr = "bid_px" if side == "bid" else "ask_px"
        oid = getattr(self._open, oid_attr)
        if oid is None:
            return
        if oid != -1 and not self.dry_run:
            try:
                self.exchange.cancel(self.cfg.coin, oid)
            except Exception as e:
                self.journal.write(
                    "maker_cancel_failed",
                    coin=self.cfg.coin,
                    oid=oid,
                    reason=reason,
                    error=str(e),
                )
                log.exception("maker: cancel failed coin=%s oid=%s", self.cfg.coin, oid)
                return
        setattr(self._open, oid_attr, None)
        setattr(self._open, px_attr, 0.0)
        self.journal.write("maker_cancel_one", coin=self.cfg.coin, side=side, reason=reason)

    def _cancel_all(self, reason: str) -> None:
        """Cancel any open quotes.

        Only clears the local oid/px AFTER a successful cancel. If
        exchange.cancel() raises (transient HL failure), the oid is kept
        tracked so the next _cancel_all retries the cancel and the order is
        never silently orphaned.
        """
        for oid_attr, px_attr in (("bid_oid", "bid_px"), ("ask_oid", "ask_px")):
            oid = getattr(self._open, oid_attr)
            if oid is None:
                continue
            if oid != -1 and not self.dry_run:
                try:
                    self.exchange.cancel(self.cfg.coin, oid)
                except Exception as e:
                    # Keep the oid tracked: the order is still resting on the
                    # exchange. Clearing it here would orphan a live order
                    # (double exposure / orders resting past expiry).
                    self.journal.write(
                        "maker_cancel_failed",
                        coin=self.cfg.coin,
                        oid=oid,
                        reason=reason,
                        error=str(e),
                    )
                    log.exception("maker: cancel failed coin=%s oid=%s", self.cfg.coin, oid)
                    continue
            setattr(self._open, oid_attr, None)
            setattr(self._open, px_attr, 0.0)
        self.journal.write("maker_cancel_all", coin=self.cfg.coin, reason=reason)

    @staticmethod
    def _extract_oid(result: Any) -> int | None:
        """Pull oid from HL's order response shape."""
        try:
            statuses = result.get("response", {}).get("data", {}).get("statuses", [])
            for s in statuses:
                if isinstance(s, dict):
                    if "resting" in s:
                        return int(s["resting"]["oid"])
                    if "filled" in s:
                        return int(s["filled"]["oid"])
        except (AttributeError, KeyError, TypeError, ValueError):
            return None
        return None


def _assert_live_account_safety(*, dry_run, arg_account, cfg_account, signer_addr,
                                agent_addrs, kill_file, cfg_kill_file):
    """Refuse to place LIVE orders unless the target account, signing key, and kill
    file are explicitly a SEPARATE subaccount setup. Prevents a mislaunch (omitted
    --account-address, or the master's env) from trading the mirror bot's account.
    Dry-run is exempt — it places no orders. Raises SystemExit on any unsafe config."""
    if dry_run:
        return
    if not arg_account:
        raise SystemExit(
            "maker LIVE refused: --account-address must be set explicitly "
            "(no silent fallback to the config/master account).")
    if cfg_account and arg_account.lower() == cfg_account.lower():
        raise SystemExit(
            "maker LIVE refused: --account-address equals the config/master account. "
            "Trade a SEPARATE subaccount, never the mirror bot's account.")
    if signer_addr.lower() not in {a.lower() for a in agent_addrs}:
        raise SystemExit(
            f"maker LIVE refused: signing key ({signer_addr[:10]}...) is NOT an authorized "
            f"agent of {arg_account[:10]}.... Wrong key for this account.")
    if kill_file == cfg_kill_file:
        raise SystemExit(
            "maker LIVE refused: --kill-file must differ from the mirror bot's kill "
            "switch, else killing one affects the other.")


def main() -> int:
    """Standalone entrypoint:

    .venv/bin/python -m src.maker --coin #20 --expiry 2026-05-06T06:00:00+00:00
    .venv/bin/python -m src.maker --coin #20 --expiry 2026-05-06T06:00:00+00:00 --dry-run
    """
    import argparse
    from datetime import datetime

    from eth_account import Account
    from hyperliquid.exchange import Exchange
    from hyperliquid.info import Info

    from .config import load_config
    from .hl_outcome import register_outcome_assets
    from .log import setup_logging

    p = argparse.ArgumentParser(prog="hyper-trader-maker")
    p.add_argument("--config", default="config.yaml")
    p.add_argument("--coin", required=True, help="Outcome coin name, e.g. #20")
    p.add_argument("--expiry", required=True, help="ISO8601 expiry, e.g. 2026-05-06T06:00:00+00:00")
    p.add_argument(
        "--min-spread-bps", type=float, default=30.0, help="Spread floor (default 30 bps)"
    )
    p.add_argument("--quote-size", type=float, default=1.0, help="Shares per side")
    p.add_argument("--max-position", type=float, default=20.0, help="Max long shares")
    p.add_argument("--max-inventory-usd", type=float, default=5.0, help="Max $ at risk")
    p.add_argument("--dry-run", action="store_true", help="Don't submit real orders")
    p.add_argument("--account-address", default=None,
                   help="Trade this account/subaccount instead of config's (signed by the "
                        "same key). Use to run the maker on a separate HL subaccount, "
                        "isolated from the mirror bot's account.")
    p.add_argument("--kill-file", default=None,
                   help="Kill-switch file for THIS maker (default: config's kill_switch_file). "
                        "Give the maker its own so it doesn't share the mirror bot's ./KILL.")
    args = p.parse_args()

    cfg = load_config(args.config)
    setup_logging(level=cfg.ops.log_level, json_mode=cfg.ops.log_json)

    # Subaccount isolation: let the maker target its own HL subaccount and its
    # own kill-switch file, so it never collides with the live mirror bot's
    # account or ./KILL. Signing key stays the master wallet (cfg.private_key);
    # HL routes orders to `acct` when it's a subaccount the key controls.
    acct = args.account_address or cfg.account_address
    kill_file = args.kill_file or cfg.risk.kill_switch_file

    # skip_ws=False: we need the WS to receive our own fills (userFills). REST
    # (info.post) still works for l2Book polling regardless.
    info = Info(cfg.hyperliquid_api_url, skip_ws=False)
    register_outcome_assets(info)
    market_meta = MarketMeta(info)
    market_meta.load()

    journal = Journal(cfg.ops.journal_path)
    wallet = Account.from_key(cfg.private_key)
    exchange = Exchange(wallet, cfg.hyperliquid_api_url, account_address=acct)

    # LIVE-safety gate: never trade the master account or with an unauthorized key.
    _agents = []
    try:
        _ea = info.post("/info", {"type": "extraAgents", "user": acct}) or []
        _agents = [a.get("address", "") for a in _ea if isinstance(a, dict)]
    except Exception:
        _agents = []
    _assert_live_account_safety(
        dry_run=args.dry_run, arg_account=args.account_address,
        cfg_account=cfg.account_address, signer_addr=wallet.address,
        agent_addrs=_agents, kill_file=kill_file, cfg_kill_file=cfg.risk.kill_switch_file)
    register_outcome_assets(exchange.info)

    expiry_ts = int(datetime.fromisoformat(args.expiry).timestamp())
    mk_cfg = MakerConfig(
        coin=args.coin,
        expiry_ts=expiry_ts,
        quote_size_shares=args.quote_size,
        min_spread_bps=args.min_spread_bps,
        max_position_shares=args.max_position,
        max_inventory_usd=args.max_inventory_usd,
        kill_switch_file=kill_file,
    )
    maker = OutcomeMaker(
        info=info,
        exchange=exchange,
        market_meta=market_meta,
        journal=journal,
        config=mk_cfg,
        dry_run=args.dry_run,
        account_address=acct,
    )

    # Wire own-fills → inventory tracking. Without this the maker is blind to
    # its own fills and never knows it holds a position. Even in dry-run we
    # subscribe (harmless: no real fills arrive), so the live/dry paths match.
    info.subscribe(
        {"type": "userFills", "user": acct},
        maker.handle_ws_fills,
    )

    # Public trades tape → toxicity / flow-imbalance defense (TFI). Subscribe
    # even in dry-run so live/dry paths match; the handler is inert when the
    # toxicity gate is disabled (nothing reads the tape).
    info.subscribe(
        {"type": "trades", "coin": args.coin},
        maker.handle_ws_trades,
    )

    maker.run()
    return 0


if __name__ == "__main__":
    import sys

    sys.exit(main())
