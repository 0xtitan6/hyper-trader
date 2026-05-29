"""Leader-edge backtest: simulate copy-trading our followed leaders over a
historical window using our actual sizing config.

Question this is built to answer: of the leaders we currently follow, which
ones have actually produced edge over the last N days IF we'd mirrored every
fill perfectly at their price with our sizing rules?

Methodology:
  - Pull each leader's `userFillsByTime` history from HL
  - Filter to perp fills only (matches our `allowed_market_types: [perp]`)
  - Filter to non-snapshot, non-spot-pair, non-outcome fills
  - For each fill, compute simulated mirror size:
      our_notional = min(
          proportional_fraction * account_proxy * weight,
          max_per_trade_usd
      )
      our_size_ratio = our_notional / leader_notional
  - Scale the leader's `closedPnl` and `fee` by our_size_ratio to estimate
    our P&L if we'd mirrored
  - Sum per-leader, report stats: trades, hit rate, Sharpe, total realized

Honest caveats:
  - This is **in-sample for selection** — our leaders were chosen because
    they appear on Liquidiction's leaderboard, so their historical PnL
    is positively biased
  - Ignores the conflict-lock (PR #25): could have skipped some mirrors in
    practice that this counts
  - Ignores cap-binding: assumes we always had room to take the trade
  - Assumes our fill at the leader's exact price (no slippage, no missed
    timing, no IOC partial fills) — real mirror has ~10-50 bps drift
  - Assumes funding-aware sizing OFF for simplicity

The answer this script gives is closer to an UPPER BOUND on our strategy's
edge — if it shows weak/negative results, the real strategy is worse.
"""

from __future__ import annotations

import argparse
import json
import statistics
import time
import urllib.parse
import urllib.request
from collections import defaultdict
from dataclasses import dataclass


@dataclass
class LeaderResult:
    address: str
    weight: float
    trades: int            # fills counted (perp, non-snapshot)
    closing_fills: int     # fills with non-zero closedPnl
    hit_rate: float        # fraction of closing fills that were positive
    total_realized: float  # our simulated PnL
    total_fees: float      # our simulated fees
    net: float             # total_realized - total_fees
    avg_trade: float       # mean closedPnl across closing fills
    median_trade: float
    pnl_std: float         # standard deviation of closing fill PnL
    sharpe: float          # mean / std (per-trade, not annualized)
    largest_win: float
    largest_loss: float
    max_drawdown: float    # worst peak-to-trough on cumulative realized
    coins: dict[str, float]  # per-coin realized contribution


def fetch_fills(address: str, since_ms: int, max_retries: int = 4) -> list[dict]:
    """Fetch userFillsByTime with simple exponential backoff on 429."""
    payload = {"type": "userFillsByTime", "user": address.lower(), "startTime": since_ms}
    body = json.dumps(payload).encode()
    for attempt in range(max_retries):
        try:
            req = urllib.request.Request(
                "https://api.hyperliquid.xyz/info",
                data=body,
                headers={"Content-Type": "application/json"},
            )
            with urllib.request.urlopen(req, timeout=15) as resp:
                return json.loads(resp.read())
        except urllib.error.HTTPError as e:
            if e.code == 429 and attempt + 1 < max_retries:
                time.sleep(1.5 * (2**attempt))
                continue
            raise
    return []


def is_perp(coin: str) -> bool:
    """Match the bot's perp filter — exclude outcomes (#), spot (@, /)."""
    if not coin:
        return False
    if coin.startswith("#") or coin.startswith("+"):
        return False
    if coin.startswith("@") or "/" in coin:
        return False
    return True


def simulate_leader(
    address: str,
    weight: float,
    fills: list[dict],
    proportional_fraction: float,
    account_proxy: float,
    max_per_trade_usd: float,
    min_per_trade_usd: float,
) -> LeaderResult:
    """Compute per-leader simulated mirror stats from raw HL fills.

    `account_proxy` is the notional sizing base — for our actual config this
    is `total_account_value`. We use a constant ($600) for the backtest so
    we're comparing leaders on equal sizing footing.
    """
    closing_pnls: list[float] = []
    closing_fees: list[float] = []
    counted_trades = 0
    per_coin: dict[str, float] = defaultdict(float)

    target_notional = min(proportional_fraction * account_proxy * weight, max_per_trade_usd)
    target_notional = max(target_notional, min_per_trade_usd)

    for f in fills:
        if not isinstance(f, dict):
            continue
        coin = f.get("coin", "")
        if not is_perp(coin):
            continue
        try:
            leader_sz = float(f.get("sz", 0))
            leader_px = float(f.get("px", 0))
            leader_pnl = float(f.get("closedPnl", 0))
            leader_fee = float(f.get("fee", 0))
        except (TypeError, ValueError):
            continue
        if leader_sz <= 0 or leader_px <= 0:
            continue
        leader_notional = leader_sz * leader_px
        if leader_notional <= 0:
            continue
        # Our sizing ratio against the leader. Floor at fraction-of-target so
        # we don't over-amplify tiny leader trades; cap so we don't exceed
        # what max_per_trade allows.
        ratio = min(target_notional / leader_notional, 1.0)
        counted_trades += 1
        # PnL scales linearly with our size ratio (positions size proportionally,
        # price moves are the same percent for everyone)
        our_pnl = leader_pnl * ratio
        our_fee = leader_fee * ratio
        if leader_pnl != 0:
            closing_pnls.append(our_pnl)
            closing_fees.append(our_fee)
            per_coin[coin] += our_pnl - our_fee

    closing_fills = len(closing_pnls)
    total_realized = sum(closing_pnls)
    total_fees = sum(closing_fees)
    net = total_realized - total_fees

    if closing_fills >= 1:
        wins = sum(1 for p in closing_pnls if p > 0)
        hit_rate = wins / closing_fills
        avg_trade = statistics.mean(closing_pnls)
        median_trade = statistics.median(closing_pnls)
        pnl_std = statistics.pstdev(closing_pnls) if closing_fills >= 2 else 0.0
        sharpe = (avg_trade / pnl_std) if pnl_std > 0 else 0.0
        largest_win = max(closing_pnls)
        largest_loss = min(closing_pnls)
        # max drawdown on cumulative realized series
        cum = 0.0
        peak = 0.0
        max_dd = 0.0
        for p in closing_pnls:
            cum += p
            if cum > peak:
                peak = cum
            dd = peak - cum
            if dd > max_dd:
                max_dd = dd
    else:
        hit_rate = avg_trade = median_trade = pnl_std = sharpe = 0.0
        largest_win = largest_loss = max_dd = 0.0

    return LeaderResult(
        address=address,
        weight=weight,
        trades=counted_trades,
        closing_fills=closing_fills,
        hit_rate=hit_rate,
        total_realized=total_realized,
        total_fees=total_fees,
        net=net,
        avg_trade=avg_trade,
        median_trade=median_trade,
        pnl_std=pnl_std,
        sharpe=sharpe,
        largest_win=largest_win,
        largest_loss=largest_loss,
        max_drawdown=max_dd,
        coins=dict(sorted(per_coin.items(), key=lambda kv: -abs(kv[1]))),
    )


def format_report(results: list[LeaderResult]) -> str:
    """Pretty per-leader table + portfolio summary."""
    if not results:
        return "(no results)"
    lines = []
    lines.append(
        f"{'address':14s}  {'wt':>4s}  {'trades':>6s}  {'closes':>6s}  "
        f"{'hit%':>5s}  {'net$':>8s}  {'avg$':>7s}  {'sharpe':>6s}  {'maxDD$':>7s}  top coin"
    )
    lines.append("-" * 110)
    portfolio_net = 0.0
    for r in results:
        portfolio_net += r.net
        top_coin = next(iter(r.coins), "")
        top_pnl = r.coins.get(top_coin, 0.0) if top_coin else 0.0
        top_str = f"{top_coin}({top_pnl:+.1f})" if top_coin else ""
        lines.append(
            f"{r.address[:14]}  {r.weight:>4.2f}  {r.trades:>6d}  {r.closing_fills:>6d}  "
            f"{r.hit_rate*100:>4.0f}%  {r.net:>+7.2f}  {r.avg_trade:>+6.2f}  "
            f"{r.sharpe:>+5.2f}  {r.max_drawdown:>6.2f}  {top_str}"
        )
    lines.append("-" * 110)
    lines.append(f"{'PORTFOLIO TOTAL':>89s}  net=${portfolio_net:+.2f}")
    return "\n".join(lines)


def per_leader_breakdown(results: list[LeaderResult]) -> str:
    """Detailed per-leader coin contributions, useful for spotting which
    coins each leader actually earns on."""
    lines = []
    for r in results:
        lines.append(f"\n=== {r.address[:14]}  weight={r.weight:.2f}  net=${r.net:+.2f} ===")
        for coin, pnl in r.coins.items():
            lines.append(f"  {coin:12s} {pnl:+8.2f}")
    return "\n".join(lines)


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--days", type=int, default=30, help="Lookback window (days)")
    p.add_argument("--account-proxy", type=float, default=600.0,
                   help="Sizing base ($) — held constant across leaders for comparability")
    p.add_argument("--config", default="config.yaml", help="Path to config for leader+sizing")
    p.add_argument("--detail", action="store_true", help="Print per-leader coin breakdown")
    args = p.parse_args()

    from src.config import load_config

    cfg = load_config(args.config)
    weights = {a.lower(): float(w) for a, w in (cfg.discovery.leader_weights or {}).items()}
    # Include always_follow leaders even if no explicit weight
    for a in cfg.discovery.always_follow or []:
        weights.setdefault(a.lower(), 1.0)

    if not weights:
        print("No leaders configured — set always_follow or leader_weights in config.yaml")
        return 1

    since_ms = int((time.time() - args.days * 86400) * 1000)
    print(f"Backtesting {len(weights)} leaders over {args.days}d "
          f"(account_proxy=${args.account_proxy:.0f}, prop_frac={cfg.sizing.proportional_fraction}, "
          f"max_per_trade=${cfg.sizing.max_per_trade_usd:.0f})")
    print()

    results = []
    for addr, weight in weights.items():
        try:
            fills = fetch_fills(addr, since_ms)
        except Exception as e:
            print(f"  {addr[:14]}: FAILED ({type(e).__name__}: {e})")
            continue
        if not isinstance(fills, list):
            print(f"  {addr[:14]}: bad response shape")
            continue
        r = simulate_leader(
            address=addr,
            weight=weight,
            fills=fills,
            proportional_fraction=cfg.sizing.proportional_fraction,
            account_proxy=args.account_proxy,
            max_per_trade_usd=cfg.sizing.max_per_trade_usd,
            min_per_trade_usd=cfg.sizing.min_per_trade_usd,
        )
        results.append(r)
        time.sleep(0.6)  # be polite to HL rate limits

    # Sort by net P&L
    results.sort(key=lambda r: r.net, reverse=True)
    print(format_report(results))
    if args.detail:
        print()
        print(per_leader_breakdown(results))
    return 0


if __name__ == "__main__":
    import sys

    sys.exit(main())
