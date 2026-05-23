"""Register HIP-3 perp DEX assets so the SDK can resolve their coin names.

The hyperliquid SDK's `Info(...)` defaults to loading only the original perp
DEX. HIP-3 builder-deployed dexes (xyz, flx, vntl, hyna, km, abcd, cash, para,
etc.) require explicit registration. Without this, `exchange.order("xyz:NVDA",
...)` fails with `KeyError: 'xyz:NVDA'` because the SDK's `coin_to_asset` map
doesn't include those names.

Same pattern as `src/hl_outcome.py` (which patches HIP-4 outcomes). Both
fixes work by post-construction mutation of the Info instance's coin maps.

Live cost of NOT having this: 2026-05-23 14:57 UTC — our oil specialist
(`0x8607a7d1`, weight 2.0, xyz domain expert) fired multiple xyz:NVDA buys.
Mirror submit raised KeyError on every child order. Missed signal entirely
from our highest-conviction leader on their specialty.
"""

from __future__ import annotations

import logging
from typing import Any

from .protocols import InfoProto

log = logging.getLogger(__name__)


def register_hip3_dexes(info: InfoProto) -> int:
    """Register every active HIP-3 dex's universe into `info.coin_to_asset`.

    Returns the number of assets registered. Idempotent — re-running just
    overwrites the same coin → asset_id mappings.

    Uses the SDK's internal `set_perp_meta(meta, offset)` to mutate the
    instance's maps. Offsets match the SDK's own convention:
    builder-deployed perp dexes start at asset_id 110_000 with step 10_000.

    Tolerant of partial failures: if one dex's meta fetch fails, we log and
    continue with the others.
    """
    try:
        dexes = info.post("/info", {"type": "perpDexs"}) or []
    except Exception:
        log.exception("register_hip3_dexes: perpDexs fetch failed")
        return 0
    if not isinstance(dexes, list):
        log.warning("register_hip3_dexes: perpDexs returned non-list %s", type(dexes))
        return 0

    registered = 0
    # First entry is the original dex (null). Builder-deployed dexes follow,
    # offset starts at 110000 with step 10000 per SDK constants.
    for i, dex_info in enumerate(dexes[1:]):
        if not isinstance(dex_info, dict):
            continue
        name = dex_info.get("name")
        if not name:
            continue
        offset = 110_000 + i * 10_000
        try:
            meta: Any = info.post("/info", {"type": "meta", "dex": name})
        except Exception:
            log.exception("register_hip3_dexes: meta fetch failed for dex=%s", name)
            continue
        if not isinstance(meta, dict) or "universe" not in meta:
            log.warning("register_hip3_dexes: bad meta shape for dex=%s", name)
            continue
        # set_perp_meta is the SDK's blessed way to add assets post-construction.
        # We call it through the SDK's own method so future SDK changes propagate.
        try:
            info.set_perp_meta(meta, offset)  # type: ignore[attr-defined]
        except AttributeError:
            # SDK changed — fall back to manual map mutation
            universe = meta.get("universe", [])
            for asset_idx, asset_info in enumerate(universe):
                if not isinstance(asset_info, dict):
                    continue
                coin = asset_info.get("name")
                if not coin:
                    continue
                asset_id = offset + asset_idx
                info.coin_to_asset[coin] = asset_id  # type: ignore[attr-defined]
                info.name_to_coin[coin] = coin  # type: ignore[attr-defined]
                sz_decimals = asset_info.get("szDecimals")
                if sz_decimals is not None:
                    info.asset_to_sz_decimals[asset_id] = sz_decimals  # type: ignore[attr-defined]
        count = len(meta.get("universe", []) or [])
        registered += count
        log.info("register_hip3_dexes: registered %d assets from dex=%s (offset=%d)", count, name, offset)
    return registered
