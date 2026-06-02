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
from .funding_history import FundingHistory
from .leader_reconcile import LeaderReconciler
from .pref_client import PrefClient
from .thesis import ThesisCache
from .thesis_generator import ThesisGenerator
from .ws_health import WSHealthMonitor
from .funding import FundingTracker
from .hl_hip3 import register_hip3_dexes
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

    # Register HIP-3 builder-deployed perp dexes (xyz, flx, vntl, etc.) so
    # Exchange.order("xyz:NVDA", ...) resolves. Same patch pattern as
    # outcomes — SDK's Info() defaults to original dex only.
    n_hip3 = register_hip3_dexes(info)
    log.info("Registered %d HIP-3 perp assets for trading", n_hip3)

    state = State(cfg.ops.state_db)
    journal = Journal(cfg.ops.journal_path)
    alerter = build_alerter(cfg)

    wallet = Account.from_key(cfg.private_key)
    exchange = Exchange(wallet, cfg.hyperliquid_api_url, account_address=cfg.account_address)
    # Exchange spawns its own internal Info — patch THAT too, otherwise
    # exchange.order("#NN", ...) still fails despite our outer info patch.
    register_outcome_assets(exchange.info)
    register_hip3_dexes(exchange.info)

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

    # WSHealthMonitor wraps info.subscribe so we track every WS subscription
    # and can rebuild + replay them if the underlying connection silently
    # dies. Routes all downstream subscribe() calls (positions, follower)
    # through the monitor transparently. See src/ws_health.py for the
    # silent-degrade bug we're fixing here.
    ws_health = WSHealthMonitor(info, alerter, stale_threshold_s=cfg.ops.ws_stale_threshold_s * 2)
    info.subscribe = ws_health.subscribe  # type: ignore[method-assign]
    ws_health.start()

    positions = PositionTracker(info, cfg.account_address, state, journal, health, alerter=alerter)
    # Reconcile BEFORE subscribing — the WS snapshot can be truncated and the
    # `user_state` endpoint is the authoritative source for current positions
    # (also catches HIP-4 settlement that occurred while the bot was down).
    positions.reconcile_with_user_state()
    positions.start()

    liq = LiquidictionClient(cfg.network.liquidiction_base)
    leaders = discover_leaders(liq, cfg.discovery, info=info)
    if not leaders:
        log.error("No leaders matched filters; aborting.")
        journal.write("startup_aborted", reason="no_leaders")
        health.stop()
        ws_health.stop()
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

    funding = FundingTracker(info)
    if cfg.sizing.use_funding_aware_sizing:
        n_funded = funding.refresh()
        log.info("FundingTracker primed with %d perp rates", n_funded)

    mirror = MirrorTrader(
        cfg, exchange, positions, journal, alerter, market_meta, funding=funding
    )
    mirror.update_leader_weights({t.address: t.weight for t in leaders})
    follower = FillFollower(info, mirror.on_leader_fill, state, health)
    follower.follow([t.address for t in leaders])
    follower_holder["f"] = follower

    leader_reconciler = LeaderReconciler(
        info=info,
        state=state,
        journal=journal,
        alerter=alerter,
        exchange=exchange,
        market_meta=market_meta,
        auto_close=cfg.risk.leader_exit_auto_close,
        debounce_cycles=cfg.risk.leader_exit_debounce_cycles,
        slippage_bps=cfg.sizing.ioc_slippage_bps,
        manual_holdings=cfg.risk.manual_holdings,
    )

    funding_history = FundingHistory(state)
    # Cold-start backfill of funding history; subsequent polls are incremental.
    try:
        backfilled = funding_history.poll(info, cfg.account_address)
        if backfilled:
            log.info("FundingHistory: backfilled %d events on startup", backfilled)
    except Exception:
        log.exception("FundingHistory startup backfill failed")

    # Thesis generator — writes per-coin BULL/BEAR/NEUTRAL stances to the
    # ThesisCache using funding + cross-leader concordance. No live trading
    # impact yet (MirrorTrader doesn't consult the cache); operator can
    # inspect via `python -m src.thesis_cli list` to validate output quality
    # before we wire the filter into the hot path (separate PR).
    thesis_cache = ThesisCache(state)
    # PrefClient is a soft dependency — if credentials missing or PREF is
    # down, the generator silently runs without sentiment enrichment.
    pref_client = PrefClient()
    thesis_generator = ThesisGenerator(
        info=info,
        state=state,
        cache=thesis_cache,
        leader_addresses=[t.address for t in leaders],
        ttl_s=900,  # 15 min — 50% longer than cycle so missed cycle doesn't blank cache
        pref_client=pref_client,
    )

    stop = install_signal_handler()
    last_refresh = time.time()
    last_reconcile = time.time()
    last_leader_recon = time.time()
    last_funding_poll = time.time()
    last_hip3_refresh = time.time()
    last_outcome_refresh = time.time()
    last_thesis_cycle = time.time()
    reconcile_interval_s = 300  # 5 min — picks up HIP-4 settlement + manual trades
    leader_recon_interval_s = 300  # 5 min — detect leader exits we missed via WS
    funding_poll_interval_s = 3600  # 1 hour — matches HL's funding accrual cycle
    thesis_cycle_interval_s = 600  # 10 min — generate fresh BULL/BEAR/NEUTRAL per coin
    # HIP-3 builder dexes (xyz, flx, etc.) add new symbols over time. Without
    # periodic re-registration, exchange.order("xyz:NEW_SYMBOL", ...) raises
    # KeyError on anything added after startup (live cost 2026-05-23 xyz:NVDA,
    # 2026-05-28 xyz:QNT). re-register every 30 min is cheap (2 small HTTP
    # calls per dex) and idempotent (set_perp_meta overwrites existing entries).
    hip3_refresh_interval_s = 1800
    # HIP-4 outcomes (#NN coin names) follow the same dynamic-registration
    # pattern as HIP-3. New outcome markets get added by HL between bot starts
    # (live cost 2026-06-02 #1420 NBA Finals — leader_reconcile auto-close
    # looped on KeyError every 5 min). Refresh every 10 min — outcomes are
    # created more frequently than builder-dex symbols.
    outcome_refresh_interval_s = 600
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
            if now - last_leader_recon >= leader_recon_interval_s:
                last_leader_recon = now
                try:
                    leader_reconciler.reconcile(follower.addresses)
                except Exception:
                    log.exception("Leader reconcile failed")
            if now - last_thesis_cycle >= thesis_cycle_interval_s:
                last_thesis_cycle = now
                try:
                    thesis_generator.run_cycle()
                except Exception:
                    log.exception("Thesis generator cycle failed")
            if now - last_funding_poll >= funding_poll_interval_s:
                last_funding_poll = now
                try:
                    funding_history.poll(info, cfg.account_address)
                except Exception:
                    log.exception("Funding history poll failed")
            if now - last_hip3_refresh >= hip3_refresh_interval_s:
                last_hip3_refresh = now
                try:
                    prev_count = len(getattr(info, "coin_to_asset", {}))
                    register_hip3_dexes(info)
                    register_hip3_dexes(exchange.info)
                    new_count = len(getattr(info, "coin_to_asset", {}))
                    if new_count != prev_count:
                        log.info(
                            "HIP-3 refresh: coin map %d → %d (gained %d new symbols)",
                            prev_count, new_count, new_count - prev_count,
                        )
                except Exception:
                    log.exception("HIP-3 periodic refresh failed")
            if now - last_outcome_refresh >= outcome_refresh_interval_s:
                last_outcome_refresh = now
                try:
                    prev_count = len(getattr(info, "coin_to_asset", {}))
                    register_outcome_assets(info)
                    register_outcome_assets(exchange.info)
                    new_count = len(getattr(info, "coin_to_asset", {}))
                    if new_count != prev_count:
                        log.info(
                            "Outcome refresh: coin map %d → %d (gained %d new legs)",
                            prev_count, new_count, new_count - prev_count,
                        )
                except Exception:
                    log.exception("Outcome periodic refresh failed")
            if now - last_refresh < cfg.discovery.refresh_seconds:
                continue
            last_refresh = now
            try:
                refreshed = discover_leaders(liq, cfg.discovery, info=info)
                new_addrs = {t.address for t in refreshed}
                cur_addrs = {t.address for t in leaders}
                added = new_addrs - cur_addrs
                if added:
                    log.info("Adding %d new leaders to follow set", len(added))
                    follower.follow(sorted(added))
                mirror.update_leader_weights({t.address: t.weight for t in refreshed})
                thesis_generator.update_leaders([t.address for t in refreshed])
                if cfg.sizing.use_funding_aware_sizing:
                    funding.refresh()
                leaders = refreshed
            except Exception:
                log.exception("Leader refresh failed")
    finally:
        log.info("Shutting down.")
        journal.write("shutdown")
        alerter.alert("info", "hyper-trader stopping")
        health.stop()
        ws_health.stop()
        alerter.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
