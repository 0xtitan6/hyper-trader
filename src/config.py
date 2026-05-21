import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml
from dotenv import load_dotenv


@dataclass(frozen=True)
class DiscoveryConfig:
    period: str
    top_n: int
    min_trades: int
    min_volume_usd: float
    min_pnl_usd: float
    refresh_seconds: int
    # Quality filter — opt-in. When True, candidates from Liquidiction are
    # additionally screened via leader_score.meets_quality which queries each
    # candidate's HL fills and computes holding-time, direction-consistency,
    # Sharpe, and drawdown. Catches scalpers + flip-floppers that raw PnL
    # ranking lets through.
    use_quality_filter: bool = False
    score_lookback_hours: float = 168.0  # 7d
    min_holding_time_s: float = 300.0  # 5 min — reject sub-second scalpers
    min_sharpe: float = 0.0  # only reject negative Sharpe by default
    min_direction_consistency: float = 0.55  # reject pure flip-flop (0.5 = even split)
    # Perp-bias screening: 2026-05-06 we found the HL leaderboard is dominated
    # by HIP-4 outcome traders, so a perp-only mirror watches them fire and
    # skips every fill. score_perp_only re-computes Sharpe/holding/direction
    # using perp fills only (outcome activity excluded — different timing).
    # min_perp_fraction additionally requires the leader's full fill universe
    # to be ≥ N% perp. Both default to 0/False to preserve old behavior.
    score_perp_only: bool = False
    min_perp_fraction: float = 0.0
    # Operator override: hex addresses to follow regardless of leaderboard
    # rank or quality filter. Bypasses every coarse + score filter. Useful
    # for specialists auto-discovery rejects — e.g. RWA-heavy traders whose
    # tokenized-stock activity makes them fail perp_fraction but who have a
    # real edge in xyz:* names. Counted toward the WS-sub cap:
    # top_n + len(always_follow) ≤ 9.
    always_follow: list[str] = field(default_factory=list)
    # Per-leader sizing weights. With use_sharpe_weighting=True, each leader's
    # mirror size is multiplied by clip(sharpe + 1.0, 0.5, 2.0) — so Sharpe 0
    # → 1.0x (current behavior), Sharpe 1.0 → 2.0x (cap), Sharpe -0.5 → 0.5x.
    # leader_weights overrides per-address. Useful for always_follow leaders
    # whose Sharpe wasn't computed inline (synthesized stubs default to 1.0).
    use_sharpe_weighting: bool = False
    leader_weights: dict[str, float] = field(default_factory=dict)


@dataclass(frozen=True)
class SizingConfig:
    mode: str
    proportional_fraction: float
    fixed_usd: float
    max_per_trade_usd: float
    min_per_trade_usd: float
    ioc_slippage_bps: float = 50.0  # 0.5% default
    # Per-market-type minimums (override min_per_trade_usd if set).
    # HL HIP-4 outcomes have a $10 USDH minimum order value; perps allow much
    # smaller. Setting outcome_min_per_trade_usd separately lets perp trades
    # mirror at small sizes without blocking outcome attempts.
    outcome_min_per_trade_usd: float | None = None
    # Funding-aware sizing (PR #19): adjusts perp mirror size by current
    # funding rate. When we'd GET PAID funding (e.g. shorting an asset with
    # positive APR funding), size scales up by min(1 + apr/200, amplify_cap).
    # When we'd PAY funding above skip_threshold, the trade is skipped
    # entirely (the funding cost would dominate any leader edge over our
    # typical hold time). Outcomes have no continuous funding so this only
    # affects perp fills.
    use_funding_aware_sizing: bool = False
    funding_amplify_threshold_apr_pct: float = 20.0  # below: no boost
    funding_amplify_cap: float = 1.5  # max 1.5x size on extreme positive funding
    funding_skip_threshold_apr_pct: float = 100.0  # above adverse APR: skip


@dataclass(frozen=True)
class RiskConfig:
    dry_run: bool
    max_total_exposure_usd: float
    max_daily_loss_usd: float
    allowed_market_types: list[str]
    kill_switch_file: str
    # PR #30: leader exit auto-close defense. When True the bot will submit a
    # reduce_only IOC if the originating leader has flat'd/flipped on-chain
    # while we still hold the mirror. Default False — alert-only — so the
    # operator can validate the detection logic before allowing autonomous
    # closes. `leader_exit_debounce_cycles` is the number of consecutive
    # detection cycles (5-min cadence) required before acting; 2 = ~10 min.
    leader_exit_auto_close: bool = False
    leader_exit_debounce_cycles: int = 2


@dataclass(frozen=True)
class NetworkConfig:
    hyperliquid_env: str
    liquidiction_base: str


@dataclass(frozen=True)
class OpsConfig:
    state_db: str
    journal_path: str
    log_level: str
    log_json: bool
    ws_stale_threshold_s: float
    alert_min_level: str


@dataclass(frozen=True)
class Config:
    discovery: DiscoveryConfig
    sizing: SizingConfig
    risk: RiskConfig
    network: NetworkConfig
    ops: OpsConfig
    private_key: str
    account_address: str
    webhook_url: str

    @property
    def hyperliquid_api_url(self) -> str:
        return (
            "https://api.hyperliquid.xyz"
            if self.network.hyperliquid_env == "mainnet"
            else "https://api.hyperliquid-testnet.xyz"
        )


def load_config(path: str = "config.yaml", env: dict[str, str] | None = None) -> Config:
    """Load config from YAML + environment.

    `env` defaults to os.environ. Pass an explicit dict for testability.
    Raises SystemExit on invalid configuration with a user-facing message.
    """
    if env is None:
        load_dotenv()
        env = dict(os.environ)

    raw_path = Path(path)
    if not raw_path.exists():
        raise SystemExit(f"Config file not found: {path}")
    raw = yaml.safe_load(raw_path.read_text())
    if not isinstance(raw, dict):
        raise SystemExit(f"Config file is not a mapping: {path}")

    pk = (env.get("HL_PRIVATE_KEY") or "").strip()
    addr = (env.get("HL_ACCOUNT_ADDRESS") or "").strip()
    network_env = (
        env.get("HL_NETWORK") or raw.get("network", {}).get("hyperliquid_env", "")
    ).strip()
    webhook_url = (env.get("ALERT_WEBHOOK_URL") or "").strip()

    if not pk or pk.startswith("0x0000"):
        raise SystemExit("HL_PRIVATE_KEY missing or placeholder. Edit .env first.")
    if not addr or addr.startswith("0x0000"):
        raise SystemExit("HL_ACCOUNT_ADDRESS missing or placeholder. Edit .env first.")
    if network_env not in {"testnet", "mainnet"}:
        raise SystemExit(f"HL_NETWORK must be 'testnet' or 'mainnet', got {network_env!r}")

    yaml_env = raw.get("network", {}).get("hyperliquid_env")
    if network_env != yaml_env:
        raise SystemExit(
            f"Network mismatch: HL_NETWORK={network_env} but config has "
            f"network.hyperliquid_env={yaml_env}. Refusing to start."
        )

    sizing = SizingConfig(**raw["sizing"])
    if sizing.mode not in {"proportional", "fixed"}:
        raise SystemExit(f"sizing.mode must be 'proportional' or 'fixed', got {sizing.mode!r}")
    if sizing.min_per_trade_usd > sizing.max_per_trade_usd:
        raise SystemExit("sizing.min_per_trade_usd > max_per_trade_usd")
    if not (0 <= sizing.ioc_slippage_bps <= 1000):
        raise SystemExit(
            f"sizing.ioc_slippage_bps must be in [0, 1000] bps, got {sizing.ioc_slippage_bps}"
        )
    if sizing.outcome_min_per_trade_usd is not None:
        if sizing.outcome_min_per_trade_usd <= 0:
            raise SystemExit("sizing.outcome_min_per_trade_usd must be positive")
        if sizing.outcome_min_per_trade_usd > sizing.max_per_trade_usd:
            raise SystemExit(
                "sizing.outcome_min_per_trade_usd > max_per_trade_usd "
                "— outcome trades would always be rejected"
            )
    if sizing.funding_amplify_threshold_apr_pct < 0:
        raise SystemExit("sizing.funding_amplify_threshold_apr_pct must be ≥ 0")
    if not (1.0 <= sizing.funding_amplify_cap <= 5.0):
        raise SystemExit(
            f"sizing.funding_amplify_cap must be in [1.0, 5.0], got {sizing.funding_amplify_cap}"
        )
    if sizing.funding_skip_threshold_apr_pct < sizing.funding_amplify_threshold_apr_pct:
        raise SystemExit(
            "sizing.funding_skip_threshold_apr_pct must be ≥ funding_amplify_threshold_apr_pct"
        )

    risk = RiskConfig(**raw["risk"])
    valid_markets = {"outcome", "perp", "spot"}
    bad = set(risk.allowed_market_types) - valid_markets
    if bad:
        raise SystemExit(f"risk.allowed_market_types contains invalid entries: {bad}")
    if not risk.allowed_market_types:
        raise SystemExit("risk.allowed_market_types must list at least one market type")

    discovery = DiscoveryConfig(**raw["discovery"])
    if discovery.top_n < 1 or discovery.top_n > 9:
        raise SystemExit(
            "discovery.top_n must be in [1, 9] — own userFills sub takes 1 of "
            "Hyperliquid's 10 unique-user subs per IP."
        )
    for follow_addr in discovery.always_follow:
        if not isinstance(follow_addr, str) or not (
            follow_addr.startswith("0x")
            and len(follow_addr) == 42
            and all(c in "0123456789abcdefABCDEF" for c in follow_addr[2:])
        ):
            raise SystemExit(
                f"discovery.always_follow entry not a valid 0x… address: {follow_addr!r}"
            )
    if discovery.top_n + len(discovery.always_follow) > 9:
        raise SystemExit(
            f"discovery.top_n ({discovery.top_n}) + len(always_follow) "
            f"({len(discovery.always_follow)}) > 9 — exceeds HL's 10-sub cap "
            f"per IP (own userFills sub takes 1)."
        )
    if discovery.period not in {"24h", "7d", "30d", "all"}:
        raise SystemExit(
            f"discovery.period must be one of 24h|7d|30d|all, got {discovery.period!r}"
        )
    if discovery.score_lookback_hours <= 0:
        raise SystemExit(
            f"discovery.score_lookback_hours must be > 0, got {discovery.score_lookback_hours}"
        )
    if discovery.min_holding_time_s < 0:
        raise SystemExit("discovery.min_holding_time_s must be ≥ 0")
    if not (0.5 <= discovery.min_direction_consistency <= 1.0):
        raise SystemExit(
            "discovery.min_direction_consistency must be in [0.5, 1.0] "
            "(0.5 = even buy/sell mix, 1.0 = pure directional)"
        )
    if not (0.0 <= discovery.min_perp_fraction <= 1.0):
        raise SystemExit(
            f"discovery.min_perp_fraction must be in [0.0, 1.0], "
            f"got {discovery.min_perp_fraction}"
        )
    for weight_addr, weight_val in discovery.leader_weights.items():
        if not isinstance(weight_addr, str) or not (
            weight_addr.startswith("0x")
            and len(weight_addr) == 42
            and all(c in "0123456789abcdefABCDEF" for c in weight_addr[2:])
        ):
            raise SystemExit(
                f"discovery.leader_weights key not a valid 0x… address: {weight_addr!r}"
            )
        if not isinstance(weight_val, int | float) or not (0.1 <= float(weight_val) <= 5.0):
            raise SystemExit(
                f"discovery.leader_weights[{weight_addr!r}] must be in [0.1, 5.0], "
                f"got {weight_val!r}"
            )

    ops_raw = raw.get("ops") or {}
    ops = OpsConfig(
        state_db=ops_raw.get("state_db", "./state/state.db"),
        journal_path=ops_raw.get("journal_path", "./state/journal.jsonl"),
        log_level=ops_raw.get("log_level", "INFO"),
        log_json=bool(ops_raw.get("log_json", False)),
        ws_stale_threshold_s=float(ops_raw.get("ws_stale_threshold_s", 120.0)),
        alert_min_level=ops_raw.get("alert_min_level", "warn"),
    )
    if ops.alert_min_level not in {"info", "warn", "error", "critical"}:
        raise SystemExit(f"ops.alert_min_level invalid: {ops.alert_min_level!r}")
    if ops.log_level.upper() not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
        raise SystemExit(f"ops.log_level invalid: {ops.log_level!r}")

    return Config(
        discovery=discovery,
        sizing=sizing,
        risk=risk,
        network=NetworkConfig(**raw["network"]),
        ops=ops,
        private_key=pk,
        account_address=addr,
        webhook_url=webhook_url,
    )
