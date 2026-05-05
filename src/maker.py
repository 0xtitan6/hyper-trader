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

import logging
import os
import time
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
    # Inventory skew — when long, we lean ask down to encourage selling.
    # Skew is `(inventory / max_position) * skew_bps_at_full`.
    inventory_skew_bps_at_full: float = 20.0
    # Cancel-replace cadence
    cancel_threshold_bps: float = 5.0  # cancel + replace if mid moves ≥ this
    refresh_interval_s: float = 2.0  # min time between cancel+replace cycles
    # Timing
    expiry_buffer_s: int = 300  # stop quoting this many seconds before expiry
    # Sanity
    min_quote_px: float = 0.01
    max_quote_px: float = 0.99
    kill_switch_file: str = "./KILL"


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
    ):
        self.info = info
        self.exchange = exchange
        self.market_meta = market_meta
        self.journal = journal
        self.cfg = config
        self.dry_run = dry_run
        self._lock = Lock()
        self._open = _OpenOrders()
        self._inventory_shares: float = 0.0
        self._inventory_cost: float = 0.0  # total $ paid to acquire current inventory

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

    def tick(self) -> None:
        """Single quote-cycle. Public so tests can drive it directly."""
        book = self._fetch_book()
        if book is None:
            return
        bids, asks = book["levels"][0], book["levels"][1]
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

        bid_px, ask_px = self._compute_quotes(mid, best_bid, best_ask)
        if bid_px is None and ask_px is None:
            # Both sides suppressed (caps, sanity, or crossed) — cancel anything
            # resting and wait for next tick.
            self._cancel_all("no_quote_target")
            return
        self._reconcile(bid_px, ask_px, mid)

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
        if sz <= 0 or px <= 0 or side not in ("B", "A"):
            return
        with self._lock:
            if side == "B":
                self._inventory_shares += sz
                self._inventory_cost += sz * px
            else:
                # Selling: realize PnL on shares we held.
                avg = (
                    self._inventory_cost / self._inventory_shares
                    if self._inventory_shares > 0
                    else 0.0
                )
                self._inventory_shares -= sz
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

    # ---------- internals ----------

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

    def _compute_quotes(
        self, mid: float, best_bid: float, best_ask: float
    ) -> tuple[float | None, float | None]:
        """Return (bid_px, ask_px) to quote, or None on either side to abstain.

        Order of checks (each side independently):
          1. Compute target prices with inventory skew.
          2. Round to HL ticks; refuse if quotes would cross.
          3. Apply inventory caps (suppresses a side independently).
          4. Apply sanity bounds (only against still-active sides).
        """
        position_pct = self._inventory_shares / max(1.0, self.cfg.max_position_shares)
        skew = (position_pct * self.cfg.inventory_skew_bps_at_full / 10_000.0) * mid
        proposed_bid = best_bid + (10**-5) * self.cfg.quote_offset_ticks - skew
        proposed_ask = best_ask - (10**-5) * self.cfg.quote_offset_ticks - skew
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

        if bid_px is not None:
            self._place(side="B", px=bid_px)
        if ask_px is not None:
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

    def _cancel_all(self, reason: str) -> None:
        """Cancel any open quotes."""
        for oid_attr, px_attr in (("bid_oid", "bid_px"), ("ask_oid", "ask_px")):
            oid = getattr(self._open, oid_attr)
            if oid is None:
                continue
            if oid != -1 and not self.dry_run:
                try:
                    self.exchange.cancel(self.cfg.coin, oid)
                except Exception:
                    log.exception("maker: cancel failed coin=%s oid=%s", self.cfg.coin, oid)
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
    args = p.parse_args()

    cfg = load_config(args.config)
    setup_logging(level=cfg.ops.log_level, json_mode=cfg.ops.log_json)

    info = Info(cfg.hyperliquid_api_url, skip_ws=True)
    register_outcome_assets(info)
    market_meta = MarketMeta(info)
    market_meta.load()

    journal = Journal(cfg.ops.journal_path)
    wallet = Account.from_key(cfg.private_key)
    exchange = Exchange(wallet, cfg.hyperliquid_api_url, account_address=cfg.account_address)
    register_outcome_assets(exchange.info)

    expiry_ts = int(datetime.fromisoformat(args.expiry).timestamp())
    mk_cfg = MakerConfig(
        coin=args.coin,
        expiry_ts=expiry_ts,
        quote_size_shares=args.quote_size,
        min_spread_bps=args.min_spread_bps,
        max_position_shares=args.max_position,
        max_inventory_usd=args.max_inventory_usd,
        kill_switch_file=cfg.risk.kill_switch_file,
    )
    maker = OutcomeMaker(
        info=info,
        exchange=exchange,
        market_meta=market_meta,
        journal=journal,
        config=mk_cfg,
        dry_run=args.dry_run,
    )
    maker.run()
    return 0


if __name__ == "__main__":
    import sys

    sys.exit(main())
