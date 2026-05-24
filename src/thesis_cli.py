"""Operator CLI for the coin-thesis cache.

Lets a human (or future agent) inspect, set, and clear per-coin thesis
entries that filter or amplify leader signals downstream.

Usage:
  python -m src.thesis_cli list
  python -m src.thesis_cli get BTC
  python -m src.thesis_cli set BTC bull 0.7 --ttl 3600 --reason "fear/greed=22, funding -0.01%"
  python -m src.thesis_cli clear BTC
  python -m src.thesis_cli prune
"""

from __future__ import annotations

import argparse
import datetime
import sys

from .config import load_config
from .state import State
from .thesis import SOURCE_MANUAL, VALID_STANCES, ThesisCache


def _fmt_age(unix_s: int) -> str:
    dt = datetime.datetime.fromtimestamp(unix_s, tz=datetime.timezone.utc)
    return dt.strftime("%Y-%m-%d %H:%M UTC")


def cmd_list(cache: ThesisCache, args: argparse.Namespace) -> int:
    theses = cache.all_active()
    if not theses:
        print("(no active theses)")
        return 0
    print(f"{'coin':12s} {'stance':8s} {'conf':>5s}  {'ttl_min':>7s}  {'source':12s}  generated")
    print("-" * 80)
    for t in theses:
        print(
            f"{t.coin:12s} {t.stance:8s} {t.confidence:>5.2f}  "
            f"{t.ttl_remaining_s()//60:>7d}  {t.source:12s}  {_fmt_age(t.generated_at)}"
        )
    return 0


def cmd_get(cache: ThesisCache, args: argparse.Namespace) -> int:
    t = cache.get(args.coin, include_expired=args.show_expired)
    if t is None:
        print(f"(no active thesis for {args.coin})")
        return 1
    print(f"coin:         {t.coin}")
    print(f"stance:       {t.stance}")
    print(f"confidence:   {t.confidence:.2f}")
    print(f"source:       {t.source}")
    print(f"generated_at: {_fmt_age(t.generated_at)}")
    print(f"expires_at:   {_fmt_age(t.expires_at)}")
    print(f"expired:      {t.is_expired()}")
    print(f"ttl_remaining: {t.ttl_remaining_s()}s")
    if t.evidence:
        print("evidence:")
        for k, v in t.evidence.items():
            print(f"  {k}: {v}")
    return 0


def cmd_set(cache: ThesisCache, args: argparse.Namespace) -> int:
    evidence: dict = {}
    if args.reason:
        evidence["reason"] = args.reason
    if args.fear_greed is not None:
        evidence["fear_greed"] = args.fear_greed
    if args.funding_rate is not None:
        evidence["funding_rate"] = args.funding_rate
    t = cache.set(
        coin=args.coin,
        stance=args.stance,
        confidence=args.confidence,
        ttl_s=args.ttl,
        source=SOURCE_MANUAL,
        evidence=evidence,
    )
    print(f"set {t.coin} → {t.stance} (conf={t.confidence:.2f}, ttl={t.ttl_remaining_s()}s)")
    return 0


def cmd_clear(cache: ThesisCache, args: argparse.Namespace) -> int:
    removed = cache.clear(args.coin)
    print("removed" if removed else "(nothing to remove)")
    return 0 if removed else 1


def cmd_prune(cache: ThesisCache, args: argparse.Namespace) -> int:
    n = cache.prune_expired()
    print(f"pruned {n} expired entr{'y' if n == 1 else 'ies'}")
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="thesis_cli")
    p.add_argument("--config", default="config.yaml")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("list", help="Show all active theses")

    p_get = sub.add_parser("get", help="Show one coin's thesis")
    p_get.add_argument("coin")
    p_get.add_argument("--show-expired", action="store_true",
                       help="Include the row even if past expiry")

    p_set = sub.add_parser("set", help="Set a manual thesis for one coin")
    p_set.add_argument("coin")
    p_set.add_argument("stance", choices=sorted(VALID_STANCES))
    p_set.add_argument("confidence", type=float, help="0.0 to 1.0")
    p_set.add_argument("--ttl", type=int, default=3600,
                       help="Seconds until expiry (default 3600)")
    p_set.add_argument("--reason", help="Free-text note stored in evidence")
    p_set.add_argument("--fear-greed", type=float, help="0-100 reading (stored in evidence)")
    p_set.add_argument("--funding-rate", type=float, help="Decimal rate (e.g. 0.0001 = 1bp)")

    p_clear = sub.add_parser("clear", help="Remove one coin's thesis")
    p_clear.add_argument("coin")

    sub.add_parser("prune", help="Delete all expired entries")

    args = p.parse_args(argv)
    cfg = load_config(args.config)
    state = State(cfg.ops.state_db)
    cache = ThesisCache(state)

    return {
        "list": cmd_list,
        "get": cmd_get,
        "set": cmd_set,
        "clear": cmd_clear,
        "prune": cmd_prune,
    }[args.cmd](cache, args)


if __name__ == "__main__":
    sys.exit(main())
