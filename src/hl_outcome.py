"""HIP-4 outcome trading support for the Hyperliquid Python SDK.

The official `hyperliquid-python-sdk` (v0.23 as of 2026-05) does NOT expose
the asset-ID encoding for HIP-4 outcome markets. Calling
`Exchange.order("#20", ...)` raises `KeyError: '#20'` because the SDK's
`coin_to_asset` map is built only from perp/spot/builder-perp universes.

This module patches `Info` to register HIP-4 outcome assets so the rest of
the SDK works transparently.

Encoding (per HL docs https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/asset-ids):

    encoding  = 10 * outcome_id + side
    asset_id  = 100_000_000 + encoding
    coin_name = f"#{encoding}"

Where `side` is 0 or 1, indexed against `outcomeMeta.outcomes[].sideSpecs`
(typically [{"name":"Yes"}, {"name":"No"}]).

Public API:
    register_outcome_assets(info) -> int
        Fetch outcomeMeta and inject every active outcome's (Yes, No) into
        `info.coin_to_asset` and `info.name_to_coin`. Returns count added.

    encode_outcome_asset_id(outcome_id, side) -> int
        Pure encoding for tests / external callers.
"""

from __future__ import annotations

import logging
from typing import Any

log = logging.getLogger(__name__)

OUTCOME_ASSET_BASE = 100_000_000


def encode_outcome_asset_id(outcome_id: int, side: int) -> int:
    """Compute the integer asset ID for a HIP-4 outcome leg.

    Args:
        outcome_id: HL's outcome index (from `outcomeMeta.outcomes[].outcome`)
        side: 0 or 1 (index into `sideSpecs`)
    """
    if side not in (0, 1):
        raise ValueError(f"side must be 0 or 1, got {side}")
    if outcome_id < 0:
        raise ValueError(f"outcome_id must be non-negative, got {outcome_id}")
    encoding = 10 * outcome_id + side
    return OUTCOME_ASSET_BASE + encoding


def encode_outcome_coin_name(outcome_id: int, side: int) -> str:
    """Compute the string coin name (e.g. '#20') for a HIP-4 outcome leg."""
    if side not in (0, 1):
        raise ValueError(f"side must be 0 or 1, got {side}")
    encoding = 10 * outcome_id + side
    return f"#{encoding}"


def register_outcome_assets(info: Any) -> int:
    """Fetch active HIP-4 outcomes from `outcomeMeta` and register each leg
    in `info.coin_to_asset` and `info.name_to_coin` so SDK order placement
    works for `#NN` coin names.

    Idempotent — safe to call multiple times.

    Returns the number of (outcome, side) legs registered.
    """
    try:
        resp = info.post("/info", {"type": "outcomeMeta"}) or {}
    except Exception as e:
        log.warning("register_outcome_assets: outcomeMeta fetch failed: %s", e)
        return 0
    outcomes = resp.get("outcomes", []) or []
    n = 0
    for o in outcomes:
        if not isinstance(o, dict):
            continue
        outcome_id = o.get("outcome")
        if not isinstance(outcome_id, int):
            continue
        side_specs = o.get("sideSpecs") or []
        n_sides = min(len(side_specs), 2) if side_specs else 2
        for side in range(n_sides):
            coin = encode_outcome_coin_name(outcome_id, side)
            asset_id = encode_outcome_asset_id(outcome_id, side)
            info.coin_to_asset[coin] = asset_id
            info.name_to_coin[coin] = coin
            n += 1
    log.info("register_outcome_assets: registered %d outcome legs", n)
    return n
