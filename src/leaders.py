import logging

from .config import DiscoveryConfig
from .leader_score import load_metrics, meets_quality
from .liquidiction import LiquidictionClient, Trader
from .protocols import InfoProto

log = logging.getLogger(__name__)


def discover_leaders(
    client: LiquidictionClient,
    cfg: DiscoveryConfig,
    info: InfoProto | None = None,
) -> list[Trader]:
    """Pull top traders from Liquidiction, then optionally filter through
    a per-leader quality scorer (`leader_score.meets_quality`) using their
    own fill history from HL.

    The Liquidiction filters (`min_trades`, `min_volume_usd`, `min_pnl_usd`)
    are coarse — they pass leaders with high raw PnL even if that PnL came
    from a single lucky position. The score filter catches scalpers
    (low time_between_fills) and flip-floppers (low direction_consistency).

    Pass `info=None` to skip the quality scorer (legacy behavior — useful
    for tests that don't want HL fill round-trips).
    """
    candidates = client.top_traders(period=cfg.period, n=max(cfg.top_n * 4, 50))
    coarse_filtered = [
        t
        for t in candidates
        if t.trades >= cfg.min_trades
        and t.volume >= cfg.min_volume_usd
        and t.pnl >= cfg.min_pnl_usd
    ]

    # Quality filter: only enabled when info is provided AND config requests it
    score_enabled = info is not None and cfg.use_quality_filter
    selected: list[Trader] = []
    rejected_for_score: list[tuple[Trader, str]] = []

    if score_enabled:
        assert info is not None  # narrowed by score_enabled
        for t in coarse_filtered:
            metrics = load_metrics(
                info,
                t.address,
                lookback_hours=cfg.score_lookback_hours,
                perp_only=cfg.score_perp_only,
            )
            if metrics is None:
                rejected_for_score.append((t, "metrics_unavailable"))
                continue
            ok, reason = meets_quality(
                metrics,
                min_holding_s=cfg.min_holding_time_s,
                min_sharpe=cfg.min_sharpe,
                min_realized_pnl_usd=cfg.min_pnl_usd,
                min_direction_consistency=cfg.min_direction_consistency,
                min_trades=cfg.min_trades,
                min_perp_fraction=cfg.min_perp_fraction,
            )
            if ok:
                selected.append(t)
                if len(selected) >= cfg.top_n:
                    break
            else:
                rejected_for_score.append((t, reason))
    else:
        selected = coarse_filtered[: cfg.top_n]

    # Operator override: append `always_follow` addresses regardless of any
    # filter outcome above. If the address is in the candidates pool we use
    # the real Trader record; otherwise we synthesize a minimal one (the
    # downstream FillFollower only needs the address).
    if cfg.always_follow:
        already = {t.address.lower() for t in selected}
        by_addr = {t.address.lower(): t for t in candidates}
        for raw in cfg.always_follow:
            addr = raw.lower()
            if addr in already:
                continue
            forced = by_addr.get(addr) or Trader(
                address=addr, rank=-1, pnl=0.0, trades=0, volume=0.0
            )
            selected.append(forced)
            already.add(addr)

    if selected:
        log.info(
            "Selected %d/%d leaders (period=%s, quality_filter=%s): %s",
            len(selected),
            len(candidates),
            cfg.period,
            score_enabled,
            ", ".join(
                f"{t.address[:10]}…(rank={t.rank}, pnl=${t.pnl:.0f}, trades={t.trades})"
                for t in selected
            ),
        )
        if rejected_for_score:
            log.info(
                "Quality filter rejected %d candidates. First few: %s",
                len(rejected_for_score),
                ", ".join(f"{t.address[:10]}…({reason})" for t, reason in rejected_for_score[:5]),
            )
    else:
        log.warning(
            "No leaders matched filters (candidates=%d, coarse=%d, "
            "rejected_by_score=%d, min_trades=%d, min_volume=%.0f, min_pnl=%.0f)",
            len(candidates),
            len(coarse_filtered),
            len(rejected_for_score),
            cfg.min_trades,
            cfg.min_volume_usd,
            cfg.min_pnl_usd,
        )
    return selected
