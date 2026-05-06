"""Leader-quality scoring for copy-trade candidate selection.

Liquidiction's leaderboard ranks by raw 7d PnL. That's a noisy filter — a leader
with $21K PnL from one big lucky position will rank highly but produce zero EV
when copied (their edge was the entry we missed).

This module fetches a candidate's `userFillsByTime` from HL directly and computes
metrics that predict copy-trade-ability:

  - `time_between_fills_p50_s`  — median seconds between consecutive fills.
                                   Low = scalper (we can't compete with their
                                   timing). High = swing/position trader (we
                                   can actually mirror).
  - `realized_pnl_sharpe`        — mean(closed_pnl) / std(closed_pnl) over the
                                   window. Higher = more consistent edge,
                                   not just one big winner.
  - `direction_consistency`      — max(buys, sells) / total. Higher = more
                                   directional, more conviction. Lower means
                                   they flip-flop (today's leader was flip-flopping).
  - `max_drawdown_usd`           — worst peak-to-trough on cumulative realized PnL.
                                   Useful for filtering out leaders who ride
                                   massive drawdowns.
  - `total_realized_pnl_usd`     — sum of closed_pnl over the window.
  - `trade_count`                — number of closing fills (with closedPnl != 0).

`load_metrics` returns a `LeaderMetrics` dataclass; `meets_quality()` is the
boolean filter you compose with Liquidiction's pool. Configurable thresholds
let the operator dial conviction vs. raw PnL.

Real-world motivation: 2026-05-05 we mirrored a leader with $21K 7d PnL who
was a scalper — flipped long→short within 30 min, ate $0.30 in fees, no edge.
This scorer would have caught them via low `time_between_fills_p50_s` and
low `direction_consistency`.
"""

from __future__ import annotations

import itertools
import logging
import math
import statistics
import time
from dataclasses import dataclass
from typing import Any

from .protocols import InfoProto

log = logging.getLogger(__name__)


def _is_perp_coin(coin: str) -> bool:
    """Mirror src/mirror.py's classification: perp = not outcome and not spot."""
    if not coin:
        return False
    is_outcome = coin.startswith("#") or coin.startswith("+")
    is_spot = coin.startswith("@") or "/" in coin
    return not (is_outcome or is_spot)


@dataclass(frozen=True)
class LeaderMetrics:
    """Quality metrics for a single leader address over a lookback window.

    When `perp_only=True` is passed to `_compute_metrics`, the time/sharpe/dir
    fields reflect only perp fills — useful for screening leaders for a
    perp-only mirror (their portfolio Sharpe gets diluted by HIP-4 outcomes).
    `perp_fill_fraction` is always computed over raw fills.
    """

    address: str
    lookback_hours: float
    trade_count: int
    closing_fill_count: int
    total_realized_pnl_usd: float
    realized_pnl_sharpe: float  # mean / std of closedPnl values
    time_between_fills_p50_s: float  # median seconds between consecutive fills
    direction_consistency: float  # 0.5 = perfectly mixed, 1.0 = pure directional
    max_drawdown_usd: float  # worst peak-to-trough drawdown
    perp_fill_fraction: float  # fraction of all fills on perp markets (always raw)


def load_metrics(
    info: InfoProto,
    address: str,
    lookback_hours: float = 168.0,
    perp_only: bool = False,
) -> LeaderMetrics | None:
    """Fetch fills for `address` over the last `lookback_hours` and compute metrics.

    `perp_only=True` restricts time/sharpe/direction/drawdown computations to
    perp fills only, while `perp_fill_fraction` always reflects the full fill
    universe. Returns None on fetch failure or unparseable response.
    """
    since_ms = int((time.time() - lookback_hours * 3600) * 1000)
    try:
        fills = info.post(
            "/info",
            {"type": "userFillsByTime", "user": address.lower(), "startTime": since_ms},
        )
    except Exception:
        log.exception("leader_score: userFillsByTime fetch failed for %s", address[:10])
        return None
    if not isinstance(fills, list):
        return None
    return _compute_metrics(
        address=address, lookback_hours=lookback_hours, fills=fills, perp_only=perp_only
    )


def _compute_metrics(
    *,
    address: str,
    lookback_hours: float,
    fills: list[dict[str, Any]],
    perp_only: bool = False,
) -> LeaderMetrics:
    """Pure metric computation (no I/O). Public for testing."""
    if not fills:
        return _empty(address, lookback_hours)

    # First pass: count perp vs total over all parseable fills, regardless of mode.
    raw_total = 0
    raw_perp = 0
    for f in fills:
        if not isinstance(f, dict):
            continue
        raw_total += 1
        if _is_perp_coin(f.get("coin", "")):
            raw_perp += 1
    perp_fill_fraction = raw_perp / raw_total if raw_total > 0 else 0.0

    times: list[int] = []
    closed_pnls: list[float] = []
    buy_count = 0
    sell_count = 0

    for f in fills:
        if not isinstance(f, dict):
            continue
        if perp_only and not _is_perp_coin(f.get("coin", "")):
            continue
        try:
            t = int(f.get("time", 0))
            cpnl = float(f.get("closedPnl", 0))
        except (TypeError, ValueError):
            continue
        if t > 0:
            times.append(t)
        side = f.get("side")
        if side == "B":
            buy_count += 1
        elif side == "A":
            sell_count += 1
        if cpnl != 0:
            closed_pnls.append(cpnl)

    trade_count = len(times)
    closing_fill_count = len(closed_pnls)

    # Time-between-fills: sort timestamps, take consecutive deltas in seconds.
    times.sort()
    deltas_s: list[float] = []
    for prev, curr in itertools.pairwise(times):
        delta_ms = curr - prev
        if delta_ms > 0:
            deltas_s.append(delta_ms / 1000.0)
    p50 = statistics.median(deltas_s) if deltas_s else 0.0

    # Sharpe-like on closed_pnl values
    if len(closed_pnls) >= 2:
        mean = statistics.mean(closed_pnls)
        std = statistics.pstdev(closed_pnls)
        sharpe = mean / std if std > 0 else 0.0
    else:
        sharpe = 0.0

    # Direction consistency: heavier side / total
    total_dir = buy_count + sell_count
    consistency = max(buy_count, sell_count) / total_dir if total_dir > 0 else 0.0

    # Cumulative PnL → drawdown
    total_pnl = sum(closed_pnls)
    cum = 0.0
    peak = 0.0
    max_dd = 0.0
    for p in closed_pnls:
        cum += p
        if cum > peak:
            peak = cum
        dd = peak - cum
        if dd > max_dd:
            max_dd = dd

    return LeaderMetrics(
        address=address.lower(),
        lookback_hours=lookback_hours,
        trade_count=trade_count,
        closing_fill_count=closing_fill_count,
        total_realized_pnl_usd=total_pnl,
        realized_pnl_sharpe=sharpe,
        time_between_fills_p50_s=p50,
        direction_consistency=consistency,
        max_drawdown_usd=max_dd,
        perp_fill_fraction=perp_fill_fraction,
    )


def _empty(address: str, lookback_hours: float) -> LeaderMetrics:
    return LeaderMetrics(
        address=address.lower(),
        lookback_hours=lookback_hours,
        trade_count=0,
        closing_fill_count=0,
        total_realized_pnl_usd=0.0,
        realized_pnl_sharpe=0.0,
        time_between_fills_p50_s=0.0,
        direction_consistency=0.0,
        max_drawdown_usd=0.0,
        perp_fill_fraction=0.0,
    )


def meets_quality(
    m: LeaderMetrics,
    *,
    min_holding_s: float = 300.0,  # 5 min — filter out sub-second scalpers
    min_sharpe: float = 0.0,  # only reject negative-Sharpe leaders by default
    min_realized_pnl_usd: float = 0.0,
    min_direction_consistency: float = 0.55,  # reject pure flip-flop
    min_trades: int = 10,
    max_drawdown_usd: float = math.inf,
    min_perp_fraction: float = 0.0,  # require N% of fills be on perp markets
) -> tuple[bool, str]:
    """Threshold filter. Returns (ok, reason). `reason` is empty when ok=True."""
    if m.trade_count < min_trades:
        return False, f"trades={m.trade_count} < {min_trades}"
    if m.perp_fill_fraction < min_perp_fraction:
        return False, (
            f"perp_frac={m.perp_fill_fraction:.2f} < {min_perp_fraction:.2f}"
        )
    if m.time_between_fills_p50_s < min_holding_s:
        return False, (f"holding_p50={m.time_between_fills_p50_s:.0f}s < {min_holding_s:.0f}s")
    if m.realized_pnl_sharpe < min_sharpe:
        return False, f"sharpe={m.realized_pnl_sharpe:.2f} < {min_sharpe:.2f}"
    if m.total_realized_pnl_usd < min_realized_pnl_usd:
        return False, f"pnl=${m.total_realized_pnl_usd:.0f} < ${min_realized_pnl_usd:.0f}"
    if m.direction_consistency < min_direction_consistency:
        return False, (f"direction={m.direction_consistency:.2f} < {min_direction_consistency:.2f}")
    if m.max_drawdown_usd > max_drawdown_usd:
        return False, (f"max_drawdown=${m.max_drawdown_usd:.0f} > ${max_drawdown_usd:.0f}")
    return True, ""
