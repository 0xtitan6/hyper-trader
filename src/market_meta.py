"""Caches Hyperliquid market metadata for tick/lot rounding.

Hyperliquid rejects orders that don't respect the per-asset size precision
(`szDecimals`) and the 5-significant-figure price rule. We pre-load both at
startup and round before submit.
"""

import logging
from math import floor, log10
from threading import Lock
from typing import Any

from .errors import MarketMetaError

log = logging.getLogger(__name__)

_DEFAULT_PERP_SZ_DECIMALS = 4
_DEFAULT_OUTCOME_SZ_DECIMALS = 0
_PRICE_SIG_FIGS = 5


class MarketMeta:
    """Per-coin size precision lookup, with safe defaults if a coin isn't in cache."""

    def __init__(self, info: Any):
        self.info = info
        self._lock = Lock()
        self._sz_decimals: dict[str, int] = {}
        self._loaded = False

    def load(self) -> None:
        """Fetch perp + spot metadata from the API. Idempotent."""
        with self._lock:
            if self._loaded:
                return
            perp_count = self._load_perp()
            spot_count = self._load_spot()
            self._loaded = True
        log.info(
            "MarketMeta loaded: %d perp markets, %d spot markets cached",
            perp_count,
            spot_count,
        )

    def _load_perp(self) -> int:
        try:
            meta = self.info.meta() or {}
        except Exception as e:
            raise MarketMetaError(f"failed to fetch perp meta: {e}") from e
        n = 0
        for asset in meta.get("universe", []) or []:
            name = asset.get("name")
            if not name:
                continue
            self._sz_decimals[name] = int(asset.get("szDecimals", _DEFAULT_PERP_SZ_DECIMALS))
            n += 1
        return n

    def _load_spot(self) -> int:
        try:
            spot = self.info.spot_meta() or {}
        except Exception as e:
            raise MarketMetaError(f"failed to fetch spot meta: {e}") from e
        tokens_by_idx = {t.get("index"): t for t in spot.get("tokens", []) or []}
        n = 0
        for pair in spot.get("universe", []) or []:
            name = pair.get("name")
            token_idxs = pair.get("tokens", []) or []
            if not name or not token_idxs:
                continue
            base = tokens_by_idx.get(token_idxs[0], {})
            self._sz_decimals[name] = int(base.get("szDecimals", 0))
            n += 1
        return n

    def round_size(self, coin: str, sz: float) -> float:
        """Round size to the coin's szDecimals.

        For HIP-4 outcomes (`#NN` / `+NN`), defaults to integer shares.
        For unknown coins, defaults to 4 decimals (perp default).
        """
        if sz <= 0:
            return 0.0
        decimals = self._size_decimals_for(coin)
        # Round DOWN to never exceed the leader's notional
        factor = 10**decimals
        return floor(sz * factor) / factor if factor else floor(sz)

    def round_price(self, px: float) -> float:
        """Round price to 5 significant figures (HL rule). Symmetric (no direction bias)."""
        if px <= 0:
            return px
        digits = _PRICE_SIG_FIGS - 1 - floor(log10(abs(px)))
        return round(px, max(digits, 0))

    def _size_decimals_for(self, coin: str) -> int:
        if coin.startswith("#") or coin.startswith("+"):
            return _DEFAULT_OUTCOME_SZ_DECIMALS
        return self._sz_decimals.get(coin, _DEFAULT_PERP_SZ_DECIMALS)
