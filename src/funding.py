"""Per-coin funding-rate cache for funding-aware sizing in MirrorTrader.

Hyperliquid charges/pays funding hourly on perp positions. Funding rates can
swing wildly: 2026-05-08 we observed kPEPE at +21.7% APR (longs pay shorts),
AZTEC at +454% APR (extreme), STABLE at -147% APR (shorts pay longs).

When the bot mirrors a leader fill, the leader's "edge" assumes they'll
exit before funding becomes a major cost. Our hold times are typically
longer (we're slower to detect leader exits), so we eat more funding.

This module fetches funding rates for all perps, caches them with a TTL,
and exposes `get_apr_pct(coin)` for MirrorTrader to consult during sizing.

Design notes:
  - Refresh is explicit (called from main's leader-refresh loop, every 10 min)
    rather than auto-async — keeps the threading model simple.
  - Returns None for outcome (#NN) and spot (@NN/SLASH) coins; they don't
    have continuous funding (outcomes settle once at expiry).
  - Cache survives even if a single refresh fails; we just keep using the
    last good snapshot rather than blocking trades on a transient API error.
"""

from __future__ import annotations

import logging
import time

from .protocols import InfoProto

log = logging.getLogger(__name__)


class FundingTracker:
    """Caches per-perp funding rates as APR percentages.

    Refresh is explicit; call `refresh()` periodically from the discovery loop.
    `get_apr_pct(coin)` returns the cached APR or None if the coin isn't a
    perp or hasn't been seen yet.
    """

    def __init__(self, info: InfoProto):
        self._info = info
        self._cache: dict[str, float] = {}
        self._last_refresh_ts: float = 0.0

    def refresh(self) -> int:
        """Fetch funding rates for every perp; return number cached."""
        try:
            data = self._info.meta_and_asset_ctxs()
        except Exception:
            log.exception("FundingTracker.refresh: meta_and_asset_ctxs failed")
            return len(self._cache)
        if not isinstance(data, list) or len(data) < 2:
            log.warning("FundingTracker.refresh: unexpected response shape")
            return len(self._cache)
        universe = data[0].get("universe", []) if isinstance(data[0], dict) else []
        ctxs = data[1] if isinstance(data[1], list) else []
        new_cache: dict[str, float] = {}
        for u, c in zip(universe, ctxs, strict=False):
            if not isinstance(u, dict) or not isinstance(c, dict):
                continue
            coin = u.get("name", "")
            if not coin:
                continue
            try:
                hourly = float(c.get("funding", 0))
            except (TypeError, ValueError):
                continue
            apr_pct = hourly * 24 * 365 * 100  # hourly rate -> APR percentage
            new_cache[coin] = apr_pct
        if new_cache:
            self._cache = new_cache
            self._last_refresh_ts = time.time()
        return len(self._cache)

    def get_apr_pct(self, coin: str) -> float | None:
        """Return APR % for coin, or None if not cached/applicable.

        Outcomes (#NN, +NN) and spot pairs (@NN, contains "/") return None —
        they don't have continuous funding.
        """
        if not coin:
            return None
        if coin.startswith(("#", "+", "@")) or "/" in coin:
            return None
        return self._cache.get(coin)

    @property
    def cache_age_s(self) -> float:
        return time.time() - self._last_refresh_ts if self._last_refresh_ts else float("inf")
