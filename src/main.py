import argparse
import logging
import signal
import sys
import threading
import time

from eth_account import Account
from hyperliquid.exchange import Exchange
from hyperliquid.info import Info

from .alerts import Alerter, NullAlerter, WebhookAlerter
from .config import Config, load_config
from .connection import ConnectionHealth
from .errors import PreflightError
from .follower import FillFollower
from .hl_outcome import register_outcome_assets
from .journal import Journal
from .leaders import discover_leaders
from .liquidiction import LiquidictionClient
from .log import setup_logging
from .market_meta import MarketMeta
from .mirror import MirrorTrader
from .positions import PositionTracker
from .preflight import format_report, run_preflight
from .state import State

log = logging.getLogger(__name__)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="hyper-trader", description="HIP-4 copy-trading bot.")
    p.add_argument(
        "--config", default="config.yaml", help="path to config YAML (default: config.yaml)"
    )
    p.add_argument("--preflight", action="store_true", help="run preflight checks and exit")
    p.add_argument(
        "--skip-preflight", action="store_true", help="skip preflight gate (not recommended)"
    )
    return p.parse_args(argv)


def build_alerter(cfg: Config) -> Alerter:
    if cfg.webhook_url:
        return WebhookAlerter(cfg.webhook_url, min_level=cfg.ops.alert_min_level)
    return NullAlerter()


def install_signal_handler() -> threading.Event:
    stop = threading.Event()

    def handler(signum: int, _frame: object) -> None:
        log.info("Received signal %d; initiating shutdown", signum)
        stop.set()

    signal.signal(signal.SIGINT, handler)
    signal.signal(signal.SIGTERM, handler)
    return stop


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    cfg = load_config(args.config)
    setup_logging(level=cfg.ops.log_level, json_mode=cfg.ops.log_json)

    log.info(
        "hyper-trader starting env=%s dry_run=%s account=%s",
        cfg.network.hyperliquid_env,
        cfg.risk.dry_run,
        cfg.account_address,
    )

    info = Info(cfg.hyperliquid_api_url, skip_ws=bool(args.preflight))

    # Preflight gate (always runs unless explicitly skipped)
    if not args.skip_preflight or args.preflight:
        report = run_preflight(info, cfg.account_address)
        print(format_report(report))
        if args.preflight:
            return 0 if report.healthy else 1
        if not report.healthy:
            log.error("Preflight unhealthy; aborting startup. Use --skip-preflight to bypass.")
            raise PreflightError("preflight failed; see report above")

    market_meta = MarketMeta(info)
    market_meta.load()

    # Register HIP-4 outcome assets so Exchange.order("#NN", ...) resolves —
    # the upstream SDK omits these from coin_to_asset.
    n_outcomes = register_outcome_assets(info)
    log.info("Registered %d HIP-4 outcome legs for trading", n_outcomes)

    state = State(cfg.ops.state_db)
    journal = Journal(cfg.ops.journal_path)
    alerter = build_alerter(cfg)

    wallet = Account.from_key(cfg.private_key)
    exchange = Exchange(wallet, cfg.hyperliquid_api_url, account_address=cfg.account_address)
    # Exchange spawns its own internal Info — patch THAT too, otherwise
    # exchange.order("#NN", ...) still fails despite our outer info patch.
    register_outcome_assets(exchange.info)

    # Backfill closure — wired into ConnectionHealth so a stale WS triggers a REST sweep
    follower_holder: dict[str, FillFollower] = {}

    def on_stale() -> None:
        f = follower_holder.get("f")
        if f is None:
            return
        try:
            f.backfill()
        except Exception:
            log.exception("Backfill on stale failed")

    health = ConnectionHealth(
        alerter,
        stale_threshold_s=cfg.ops.ws_stale_threshold_s,
        on_stale=on_stale,
    )
    health.start()

    positions = PositionTracker(info, cfg.account_address, state, journal, health)
    # Reconcile BEFORE subscribing — the WS snapshot can be truncated and the
    # `user_state` endpoint is the authoritative source for current positions
    # (also catches HIP-4 settlement that occurred while the bot was down).
    positions.reconcile_with_user_state()
    positions.start()

    liq = LiquidictionClient(cfg.network.liquidiction_base)
    leaders = discover_leaders(liq, cfg.discovery)
    if not leaders:
        log.error("No leaders matched filters; aborting.")
        journal.write("startup_aborted", reason="no_leaders")
        health.stop()
        return 1

    journal.write(
        "startup",
        env=cfg.network.hyperliquid_env,
        dry_run=cfg.risk.dry_run,
        leaders=[t.address for t in leaders],
    )
    alerter.alert(
        "info",
        f"hyper-trader started env={cfg.network.hyperliquid_env} "
        f"dry_run={cfg.risk.dry_run} leaders={len(leaders)}",
    )

    mirror = MirrorTrader(cfg, exchange, positions, journal, alerter, market_meta)
    follower = FillFollower(info, mirror.on_leader_fill, state, health)
    follower.follow([t.address for t in leaders])
    follower_holder["f"] = follower

    stop = install_signal_handler()
    last_refresh = time.time()
    last_reconcile = time.time()
    reconcile_interval_s = 300  # 5 min — picks up HIP-4 settlement + manual trades
    log.info("Following %d leaders. Send SIGINT/SIGTERM to stop.", len(leaders))
    try:
        while not stop.is_set():
            if stop.wait(5):
                break
            now = time.time()
            if now - last_reconcile >= reconcile_interval_s:
                last_reconcile = now
                try:
                    positions.reconcile_with_user_state()
                except Exception:
                    log.exception("Periodic reconcile failed")
            if now - last_refresh < cfg.discovery.refresh_seconds:
                continue
            last_refresh = now
            try:
                refreshed = discover_leaders(liq, cfg.discovery)
                new_addrs = {t.address for t in refreshed}
                cur_addrs = {t.address for t in leaders}
                added = new_addrs - cur_addrs
                if added:
                    log.info("Adding %d new leaders to follow set", len(added))
                    follower.follow(sorted(added))
                leaders = refreshed
            except Exception:
                log.exception("Leader refresh failed")
    finally:
        log.info("Shutting down.")
        journal.write("shutdown")
        alerter.alert("info", "hyper-trader stopping")
        health.stop()
        alerter.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
