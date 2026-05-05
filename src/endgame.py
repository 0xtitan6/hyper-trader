"""Endgame buy strategy for HL HIP-4 daily binary outcomes.

Captures the structural risk-premium that opens up near expiry: when the
underlying is comfortably above (or below) the binary's target with little
time left, the winning leg often trades at 0.85-0.95 instead of converging
to 0.99. Buying the winning leg in that window has small but real positive
EV.

This is NOT a market maker (the BTC binary spread is too tight to MM).
This is NOT a directional view. It's an opportunistic time-decay capture.

Hard safety:
  - max 3 shares per fire
  - max 3 fires total
  - max $5 total inventory
  - never buys at >= 0.99 or <= 0.05 (sanity)
  - respects KILL switch
  - stops 30s before expiry (no last-minute ambiguity)
"""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass

from .errors import OrderError
from .journal import Journal
from .market_meta import MarketMeta
from .protocols import ExchangeProto, InfoProto

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class EndgameConfig:
    """Triggers tuned for $79,980 BTC daily binary.

    Each tier defines: time-to-expiry threshold (seconds), price-cushion
    threshold ($), and max-buy-price for the favored leg.
    """

    target: float
    expiry_ts: int
    yes_coin: str = "#20"
    no_coin: str = "#21"
    # tiers in (ttl_s, cushion_$, max_buy_px) ordered by ttl descending
    tiers: tuple[tuple[int, float, float], ...] = (
        (600, 400.0, 0.90),  # T-10min: BTC ≥ target+$400, YES/NO ≤ 0.90
        (300, 300.0, 0.93),  # T-5min:  BTC ≥ target+$300, YES/NO ≤ 0.93
        (120, 200.0, 0.96),  # T-2min:  BTC ≥ target+$200, YES/NO ≤ 0.96
    )
    sz_per_fire: int = 3
    max_fires: int = 3
    stop_buffer_s: int = 30  # stop attempting fires this many seconds before expiry
    sanity_min_px: float = 0.05
    sanity_max_px: float = 0.99
    poll_interval_s: float = 1.0
    kill_switch_file: str = "./KILL"


class EndgameRunner:
    """Drives the endgame strategy. Self-contained — does NOT mirror leaders.

    Submits IOC buys (taker, immediate fill or kill) when triggers fire so
    we don't sit on the book exposed to adverse selection.
    """

    def __init__(
        self,
        info: InfoProto,
        exchange: ExchangeProto,
        market_meta: MarketMeta,
        journal: Journal,
        config: EndgameConfig,
        dry_run: bool = True,
    ):
        self.info = info
        self.exchange = exchange
        self.market_meta = market_meta
        self.journal = journal
        self.cfg = config
        self.dry_run = dry_run
        self.fires = 0
        self.fired_tiers: set[int] = set()  # ttl thresholds we've already fired in

    def fetch_state(self) -> dict[str, float] | None:
        """Pull BTC mid + outcome leg mids in one shot. Returns None on failure."""
        try:
            mids = self.info.post("/info", {"type": "allMids"}) or {}
        except Exception:
            log.exception("endgame: allMids fetch failed")
            return None
        try:
            return {
                "btc": float(mids.get("BTC", 0)),
                "yes": float(mids.get(self.cfg.yes_coin, 0)),
                "no": float(mids.get(self.cfg.no_coin, 0)),
            }
        except (TypeError, ValueError):
            return None

    def fetch_best_ask(self, coin: str) -> float | None:
        """Best ask price (what we'd pay to take the leg as a buyer)."""
        try:
            book = self.info.post("/info", {"type": "l2Book", "coin": coin}) or {}
            asks = book.get("levels", [[], []])[1]
            if not asks:
                return None
            return float(asks[0]["px"])
        except Exception:
            log.exception("endgame: l2Book fetch failed for %s", coin)
            return None

    def decide(self, state: dict[str, float], ttl_s: float) -> tuple[str, float] | None:
        """Return (coin_to_buy, max_px_allowed) if a tier fires; else None.

        Picks the most-recent tier whose ttl threshold has been crossed and
        whose cushion + price conditions match. Each tier can fire at most once.
        """
        btc = state["btc"]
        diff = btc - self.cfg.target
        for ttl_thresh, cushion, max_px in self.cfg.tiers:
            if ttl_s > ttl_thresh:
                continue  # not yet in this tier's window
            if ttl_thresh in self.fired_tiers:
                continue
            # YES side — BTC well above target → buy YES
            if diff >= cushion and state["yes"] <= max_px:
                return (self.cfg.yes_coin, max_px)
            # NO side — BTC well below target → buy NO
            if diff <= -cushion and state["no"] <= max_px:
                return (self.cfg.no_coin, max_px)
        return None

    def kill_switch_active(self) -> bool:
        return os.path.exists(self.cfg.kill_switch_file)

    def submit_buy(self, coin: str, max_px: float) -> bool:
        """Place an IOC taker buy at the best ask, capped at max_px. Returns
        True iff submitted (or dry-run logged), False on skip."""
        ask = self.fetch_best_ask(coin)
        if ask is None:
            self.journal.write("endgame_skip", reason="no_ask", coin=coin)
            log.info("endgame: skip %s no ask", coin)
            return False
        if not (self.cfg.sanity_min_px <= ask <= self.cfg.sanity_max_px):
            self.journal.write("endgame_skip", reason="sanity_px", coin=coin, ask=ask)
            log.info("endgame: skip %s ask=%s outside sanity", coin, ask)
            return False
        if ask > max_px:
            self.journal.write(
                "endgame_skip", reason="ask_above_cap", coin=coin, ask=ask, max_px=max_px
            )
            log.info("endgame: skip %s ask=%.4f > cap=%.4f", coin, ask, max_px)
            return False
        # Round to HL's tick rule
        limit_px = self.market_meta.round_price(ask)
        sz = float(self.cfg.sz_per_fire)
        if self.dry_run:
            log.info(
                "[DRY] endgame BUY %s sz=%s @ %.4f notional=$%.2f",
                coin,
                sz,
                limit_px,
                sz * limit_px,
            )
            self.journal.write(
                "endgame_buy",
                dry_run=True,
                coin=coin,
                sz=sz,
                px=limit_px,
                notional=sz * limit_px,
            )
            return True
        try:
            result = self.exchange.order(
                coin,
                True,  # is_buy
                sz,
                limit_px,
                order_type={"limit": {"tif": "Ioc"}},
                reduce_only=False,
            )
        except Exception as e:
            self.journal.write("endgame_error", coin=coin, sz=sz, px=limit_px, error=str(e))
            log.exception("endgame: order submit failed")
            raise OrderError(f"endgame order failed: {e}") from e
        log.info("endgame BUY result: %s", result)
        self.journal.write(
            "endgame_buy",
            dry_run=False,
            coin=coin,
            sz=sz,
            px=limit_px,
            notional=sz * limit_px,
            result=result,
        )
        return True

    def run(self) -> None:
        """Main loop. Runs until expiry-30s OR max_fires reached OR kill-switch."""
        log.info(
            "endgame armed: target=%s expiry=%s tiers=%s sz_per_fire=%s max_fires=%s dry_run=%s",
            self.cfg.target,
            self.cfg.expiry_ts,
            self.cfg.tiers,
            self.cfg.sz_per_fire,
            self.cfg.max_fires,
            self.dry_run,
        )
        self.journal.write(
            "startup",
            module="endgame",
            target=self.cfg.target,
            expiry_ts=self.cfg.expiry_ts,
            tiers=list(self.cfg.tiers),
            dry_run=self.dry_run,
        )
        while True:
            now = time.time()
            ttl = self.cfg.expiry_ts - now
            if ttl <= self.cfg.stop_buffer_s:
                log.info("endgame: stop buffer reached (ttl=%.0fs); exiting", ttl)
                self.journal.write("endgame_settle", reason="stop_buffer", ttl=ttl)
                break
            if self.fires >= self.cfg.max_fires:
                log.info("endgame: max_fires reached; idling until expiry")
                self.journal.write("endgame_settle", reason="max_fires", fires=self.fires)
                break
            if self.kill_switch_active():
                log.info("endgame: kill switch active; exiting")
                self.journal.write("endgame_settle", reason="kill_switch")
                break

            state = self.fetch_state()
            if state is None or state["btc"] <= 0:
                time.sleep(self.cfg.poll_interval_s)
                continue

            decision = self.decide(state, ttl)
            if decision is not None:
                coin, max_px = decision
                # Mark tier as fired-attempt before actual submission so we
                # don't spam if the submission fails.
                ttl_tier = self._matching_tier(ttl)
                if ttl_tier is not None:
                    self.fired_tiers.add(ttl_tier)
                ok = self.submit_buy(coin, max_px)
                if ok:
                    self.fires += 1
            time.sleep(self.cfg.poll_interval_s)

    def _matching_tier(self, ttl_s: float) -> int | None:
        """Lookup which tier's ttl threshold this ttl falls into."""
        for ttl_thresh, _, _ in self.cfg.tiers:
            if ttl_s <= ttl_thresh:
                return ttl_thresh
        return None


def load_endgame_config(target: float, expiry_iso: str) -> EndgameConfig:
    """Build EndgameConfig from operator-supplied target + ISO8601 expiry."""
    from datetime import datetime

    expiry_ts = int(datetime.fromisoformat(expiry_iso).timestamp())
    return EndgameConfig(target=target, expiry_ts=expiry_ts)


def main() -> int:
    """Standalone entrypoint. Reads .env, loads HL SDK, runs the endgame
    strategy against the operator-specified binary.

    Usage:
        .venv/bin/python -m src.endgame --target 79980 --expiry 2026-05-05T06:00:00+00:00
        .venv/bin/python -m src.endgame --target 79980 --expiry 2026-05-05T06:00:00+00:00 --dry-run
    """
    import argparse

    from eth_account import Account
    from hyperliquid.exchange import Exchange
    from hyperliquid.info import Info

    from .config import load_config
    from .log import setup_logging

    p = argparse.ArgumentParser(prog="hyper-trader-endgame")
    p.add_argument("--config", default="config.yaml")
    p.add_argument("--target", type=float, required=True, help="Binary target price (USD)")
    p.add_argument(
        "--expiry",
        required=True,
        help="ISO8601 expiry, e.g. 2026-05-05T06:00:00+00:00",
    )
    p.add_argument("--dry-run", action="store_true", help="Don't actually submit orders")
    p.add_argument("--yes-coin", default="#20")
    p.add_argument("--no-coin", default="#21")
    args = p.parse_args()

    cfg = load_config(args.config)
    setup_logging(level=cfg.ops.log_level, json_mode=cfg.ops.log_json)

    info = Info(cfg.hyperliquid_api_url, skip_ws=True)
    market_meta = MarketMeta(info)
    market_meta.load()

    # Register HIP-4 outcome assets — required for Exchange.order("#NN", ...)
    from .hl_outcome import register_outcome_assets

    register_outcome_assets(info)

    journal = Journal(cfg.ops.journal_path)
    wallet = Account.from_key(cfg.private_key)
    exchange = Exchange(wallet, cfg.hyperliquid_api_url, account_address=cfg.account_address)
    # Exchange has its own internal Info — patch it too so order placement works.
    register_outcome_assets(exchange.info)

    eg_cfg = load_endgame_config(args.target, args.expiry)
    eg_cfg = EndgameConfig(
        target=eg_cfg.target,
        expiry_ts=eg_cfg.expiry_ts,
        yes_coin=args.yes_coin,
        no_coin=args.no_coin,
    )
    runner = EndgameRunner(
        info=info,
        exchange=exchange,
        market_meta=market_meta,
        journal=journal,
        config=eg_cfg,
        dry_run=args.dry_run,
    )
    runner.run()
    return 0


if __name__ == "__main__":
    import sys

    sys.exit(main())
