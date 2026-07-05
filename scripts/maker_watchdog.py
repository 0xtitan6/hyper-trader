#!/usr/bin/env python3
"""Liveness watchdog for the HIP-4 maker (`python -m src.maker`).

Standalone by design — imports nothing from `src/`, so it keeps running and
alerting even if the maker app is broken. Companion to scripts/watchdog.py
(which covers the mirror engine `src.main`); this one covers the maker and,
optionally, a specific `--coin` so you can run one watchdog per maker process.

Signals:
  - process DOWN   → CRITICAL (no `src.maker` [for coin] running)
  - process DOUBLE → WARN (>1 → duplicate quoting on the same book)
  - log stale      → ERROR (maker log untouched > --stale-s)

Run:
  python scripts/maker_watchdog.py --interval 60 --log state/maker.log --coin '#20'

Telegram creds come from the env / .env (TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID),
same as scripts/watchdog.py. Missing creds → alerts go to stderr only.
"""
from __future__ import annotations

import argparse
import os
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path


def find_maker_pids(coin: str | None = None) -> list[int]:
    """PIDs whose cmdline runs `python -m src.maker` (optionally for one --coin)."""
    pids: list[int] = []
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            raw = (entry / "cmdline").read_bytes()
        except (FileNotFoundError, ProcessLookupError, PermissionError):
            continue
        cmd = raw.replace(b"\x00", b" ").decode(errors="ignore")
        if "src.maker" in cmd and "python" in cmd:
            if coin is None or coin in cmd:
                pids.append(int(entry.name))
    return pids


def evaluate(
    pids: list[int], log_age_s: float | None, stale_s: float
) -> list[tuple[str, str]]:
    """Pure decision core (unit-tested): map observations → (level, message) alerts."""
    alerts: list[tuple[str, str]] = []
    if not pids:
        alerts.append(("CRITICAL", "maker DOWN — no src.maker process running"))
    elif len(pids) > 1:
        alerts.append(
            ("WARN", f"maker: {len(pids)} src.maker processes — duplicate quoting risk")
        )
    if log_age_s is not None and log_age_s > stale_s:
        alerts.append(("ERROR", f"maker log stale: untouched {log_age_s:.0f}s (> {stale_s:.0f}s)"))
    return alerts


def _telegram(level: str, message: str) -> None:
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    chat = os.environ.get("TELEGRAM_CHAT_ID", "")
    text = f"[maker-watchdog {level}] {message}"
    if not token or not chat:
        print(text, file=sys.stderr)
        return
    try:
        data = urllib.parse.urlencode({"chat_id": chat, "text": text}).encode()
        urllib.request.urlopen(
            f"https://api.telegram.org/bot{token}/sendMessage", data=data, timeout=5
        )
    except Exception as e:  # never let alerting kill the watchdog
        print(f"{text} (telegram failed: {e})", file=sys.stderr)


def log_age_s(log_path: Path, now: float) -> float | None:
    """Seconds since the log file was last written, or None if it doesn't exist."""
    if not log_path.exists():
        return None
    return now - log_path.stat().st_mtime


def check_once(log_path: Path, coin: str | None, stale_s: float, alert=_telegram) -> None:
    pids = find_maker_pids(coin)
    age = log_age_s(log_path, time.time())
    for level, msg in evaluate(pids, age, stale_s):
        alert(level, msg)


def _load_dotenv(path: Path) -> None:
    """Minimal .env loader (no dependency on python-dotenv)."""
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def main() -> int:
    p = argparse.ArgumentParser(prog="maker-watchdog")
    p.add_argument("--interval", type=int, default=60, help="seconds between checks")
    p.add_argument("--log", default="state/maker.log", help="maker log file to watch for staleness")
    p.add_argument("--coin", default=None, help="only watch the maker for this outcome coin (e.g. '#20')")
    p.add_argument("--stale-s", type=float, default=300.0, help="alert if log untouched this long")
    p.add_argument("--env", default=".env", help="path to .env for Telegram creds")
    args = p.parse_args()
    _load_dotenv(Path(args.env))
    log_path = Path(args.log)
    print(f"maker-watchdog up: interval={args.interval}s log={args.log} coin={args.coin} stale={args.stale_s}s", file=sys.stderr)
    while True:
        check_once(log_path, args.coin, args.stale_s)
        time.sleep(args.interval)


if __name__ == "__main__":
    raise SystemExit(main())
