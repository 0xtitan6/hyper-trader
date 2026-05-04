import logging

from .config import DiscoveryConfig
from .liquidiction import LiquidictionClient, Trader

log = logging.getLogger(__name__)


def discover_leaders(client: LiquidictionClient, cfg: DiscoveryConfig) -> list[Trader]:
    candidates = client.top_traders(period=cfg.period, n=max(cfg.top_n * 4, 50))
    filtered = [
        t
        for t in candidates
        if t.trades >= cfg.min_trades
        and t.volume >= cfg.min_volume_usd
        and t.pnl >= cfg.min_pnl_usd
    ]
    selected = filtered[: cfg.top_n]
    if selected:
        log.info(
            "Selected %d/%d leaders (period=%s): %s",
            len(selected),
            len(candidates),
            cfg.period,
            ", ".join(
                f"{t.address[:10]}…(rank={t.rank}, pnl=${t.pnl:.0f}, trades={t.trades})"
                for t in selected
            ),
        )
    else:
        log.warning(
            "No leaders matched filters (candidates=%d, min_trades=%d, min_volume=%.0f, min_pnl=%.0f)",
            len(candidates),
            cfg.min_trades,
            cfg.min_volume_usd,
            cfg.min_pnl_usd,
        )
    return selected
