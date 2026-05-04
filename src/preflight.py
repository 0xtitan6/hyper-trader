"""Pre-flight validation: prove HL connectivity, schema, and account access
before the bot subscribes to anything or submits any orders.

Exposes `run_preflight()` returning a PreflightReport. The CLI uses this for
`--preflight` mode; the main loop uses it as a startup gate.
"""

import logging
from dataclasses import dataclass, field
from typing import Any

log = logging.getLogger(__name__)

REQUIRED_FILL_FIELDS = frozenset({"coin", "px", "sz", "side", "tid", "time", "closedPnl", "fee"})


@dataclass
class PreflightReport:
    api_url: str
    account_address: str
    perp_markets: int = 0
    spot_markets: int = 0
    outcome_markets: int = 0
    outcomes: list[dict[str, Any]] = field(default_factory=list)
    account_reachable: bool = False
    own_fills_count: int = 0
    sample_fill: dict[str, Any] | None = None
    schema_ok: bool = False
    schema_missing: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @property
    def healthy(self) -> bool:
        return (
            self.perp_markets > 0 and self.account_reachable and self.schema_ok and not self.errors
        )


def run_preflight(info: Any, account_address: str) -> PreflightReport:
    api_url = getattr(info, "base_url", "<unknown>")
    r = PreflightReport(api_url=api_url, account_address=account_address)

    try:
        meta = info.meta() or {}
        r.perp_markets = len(meta.get("universe", []) or [])
    except Exception as e:
        r.errors.append(f"meta: {type(e).__name__}: {e}")

    try:
        spot = info.spot_meta() or {}
        r.spot_markets = len(spot.get("universe", []) or [])
    except Exception as e:
        r.errors.append(f"spot_meta: {type(e).__name__}: {e}")

    try:
        outcomes_resp = info.post("/info", {"type": "outcomeMeta"}) or {}
        r.outcomes = outcomes_resp.get("outcomes", []) or []
        r.outcome_markets = len(r.outcomes)
    except Exception as e:
        r.errors.append(f"outcomeMeta: {type(e).__name__}: {e}")

    try:
        info.user_state(account_address)
        r.account_reachable = True
    except Exception as e:
        r.errors.append(f"user_state: {type(e).__name__}: {e}")

    try:
        fills = info.user_fills(account_address) or []
        r.own_fills_count = len(fills) if isinstance(fills, list) else 0
        if r.own_fills_count > 0 and isinstance(fills[0], dict):
            r.sample_fill = fills[0]
            missing = REQUIRED_FILL_FIELDS - set(fills[0].keys())
            r.schema_missing = sorted(missing)
            r.schema_ok = not missing
        else:
            r.schema_ok = True  # nothing to validate; not a failure
    except Exception as e:
        r.errors.append(f"user_fills: {type(e).__name__}: {e}")

    return r


def format_report(r: PreflightReport) -> str:
    lines: list[str] = []
    lines.append(f"=== Hyperliquid preflight: {r.api_url} ===")
    lines.append(f"  perp markets:    {r.perp_markets}")
    lines.append(f"  spot markets:    {r.spot_markets}")
    lines.append(f"  outcome markets: {r.outcome_markets}")
    if r.outcomes:
        lines.append("  current outcomes:")
        for o in r.outcomes[:10]:
            desc = o.get("description") or o.get("name") or "?"
            lines.append(f"    - outcome={o.get('outcome')}: {desc}")
    addr = (r.account_address or "")[:10] + "…"
    lines.append(f"  account {addr}: {'reachable' if r.account_reachable else 'UNREACHABLE'}")
    lines.append(f"  own fills:       {r.own_fills_count}")
    lines.append(f"  fill schema:     {'OK' if r.schema_ok else 'MISMATCH'}")
    if r.schema_missing:
        lines.append(f"    missing fields: {r.schema_missing}")
    if r.errors:
        lines.append("  ERRORS:")
        for e in r.errors:
            lines.append(f"    ✗ {e}")
    lines.append(f"  overall:         {'HEALTHY' if r.healthy else 'UNHEALTHY'}")
    return "\n".join(lines)
