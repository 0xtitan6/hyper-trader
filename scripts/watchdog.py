#!/usr/bin/env python3
"""24/7 monitor for hyper-trader.

Runs as a systemd service alongside the bot. Polls every WATCHDOG_INTERVAL_S
(default 60s). Checks four signals — pid liveness, log freshness, journal
freshness, last reconcile timestamp — and fires Telegram alerts on state
transitions only (no spam during normal silent operation).

Design principles:
  - State-change semantics: alert on transition (good → bad), again on recovery
    (bad → good). Do NOT re-fire while still in the bad state.
  - Honor KILL switch: if `./KILL` exists, treat bot-down as expected, suppress.
  - Always tolerant of transient errors — never crash the monitor itself.
  - All Telegram POSTs go via curl-equivalent urllib with explicit form encoding
    so signs/whitespace render correctly.

Tested manually via `python3 scripts/watchdog.py --once` (single pass + exit).
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import signal
import sys
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger("watchdog")

# Detection thresholds (seconds).
LOG_STALE_THRESHOLD_S = 600       # run.log untouched 10 min → ERROR
JOURNAL_STALE_THRESHOLD_S = 1800  # journal.jsonl untouched 30 min → ERROR (reconciles every 5min so this is generous)
RECONCILE_LAG_THRESHOLD_S = 900   # last reconcile event > 15 min ago → WARN
COOLDOWN_S = 300                  # min seconds between repeat alerts of same kind

# Bot working dir (containing state/, .env, KILL).
DEFAULT_BOT_DIR = "/home/ec2-user/.openclaw/workspace/hyper-trader"


@dataclass
class WatchdogConfig:
    bot_dir: Path
    interval_s: int = 60
    webhook_url: str = ""
    chat_id: str = ""


@dataclass
class WatchdogState:
    pid_alive: bool = True
    log_fresh: bool = True
    journal_fresh: bool = True
    reconcile_fresh: bool = True
    last_alert_at: dict[str, float] = field(default_factory=dict)
    started_at: float = field(default_factory=time.time)


def _load_env(env_path: Path) -> dict[str, str]:
    env: dict[str, str] = {}
    if not env_path.exists():
        return env
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        env[k.strip()] = v.strip().strip('"').strip("'")
    return env


def find_bot_pid() -> int | None:
    """Return pid of `python -m src.main` if exactly one is running."""
    try:
        # /proc walk — no shell dep
        candidates = []
        for entry in os.listdir("/proc"):
            if not entry.isdigit():
                continue
            try:
                cmdline = Path(f"/proc/{entry}/cmdline").read_bytes().replace(b"\x00", b" ").decode()
            except (FileNotFoundError, PermissionError):
                continue
            if "src.main" in cmdline and "python" in cmdline:
                candidates.append(int(entry))
        if len(candidates) == 1:
            return candidates[0]
        if len(candidates) > 1:
            log.warning("multiple bot processes detected: %s (returning newest)", candidates)
            return max(candidates)
        return None
    except OSError:
        log.exception("find_bot_pid /proc walk failed")
        return None


def last_journal_event_age_s(journal_path: Path, event_type: str) -> float | None:
    """Return seconds since the most recent `event_type` line in the journal,
    or None if not found / read failed.

    Scans backwards from EOF — assumes journal is small enough (~MB scale) to
    read in full per cycle. For our ops this is fine.
    """
    try:
        if not journal_path.exists():
            return None
        size = journal_path.stat().st_size
        if size == 0:
            return None
        # Read last ~64KB only — cheap and almost always contains a recent event
        with open(journal_path, "rb") as f:
            f.seek(max(0, size - 65536))
            tail = f.read().decode(errors="ignore")
        # Walk lines bottom-up
        for line in reversed(tail.splitlines()):
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if obj.get("event") != event_type:
                continue
            ts = float(obj.get("ts", 0))
            if ts <= 0:
                continue
            return time.time() - ts
        return None
    except OSError:
        log.exception("journal read failed")
        return None


def telegram_alert(cfg: WatchdogConfig, level: str, message: str) -> bool:
    """POST to Telegram via urllib. Returns True if HL returned ok=true."""
    if not cfg.webhook_url or not cfg.chat_id:
        log.warning("Telegram not configured (webhook_url or chat_id missing)")
        return False
    body = urllib.parse.urlencode(
        {
            "chat_id": cfg.chat_id,
            "text": f"[watchdog {level}] {message}",
        }
    ).encode()
    req = urllib.request.Request(
        cfg.webhook_url,
        data=body,
        method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode())
        if not data.get("ok"):
            log.error("Telegram POST returned not-ok: %s", data)
            return False
        return True
    except Exception as e:
        log.error("Telegram POST failed: %s", e)
        return False


def maybe_alert(
    cfg: WatchdogConfig,
    state: WatchdogState,
    key: str,
    level: str,
    message: str,
) -> None:
    """Send a telegram alert keyed by `key` with cooldown."""
    now = time.time()
    last = state.last_alert_at.get(key, 0.0)
    if now - last < COOLDOWN_S:
        log.info("cooldown active for %s (last %.0fs ago), suppressing", key, now - last)
        return
    ok = telegram_alert(cfg, level, message)
    if ok:
        state.last_alert_at[key] = now
    else:
        log.error("failed to deliver alert key=%s message=%s", key, message)


def check_once(cfg: WatchdogConfig, state: WatchdogState) -> None:
    """One pass of all checks. Mutates `state` for transition tracking."""
    kill_file = cfg.bot_dir / "KILL"
    kill_active = kill_file.exists()
    run_log = cfg.bot_dir / "state" / "run.log"
    journal = cfg.bot_dir / "state" / "journal.jsonl"
    now = time.time()

    # 1. PID liveness
    pid = find_bot_pid()
    pid_alive = pid is not None
    if not pid_alive and not kill_active:
        if state.pid_alive:  # transition
            maybe_alert(
                cfg, state, "pid_down",
                "CRITICAL", "bot process not found AND no KILL file — bot died unexpectedly"
            )
    elif pid_alive and not state.pid_alive:
        maybe_alert(
            cfg, state, "pid_recovered",
            "INFO", f"bot recovered (pid={pid})"
        )
    state.pid_alive = pid_alive

    if not pid_alive:
        # No point checking freshness of logs from a dead bot
        log.info("bot down (kill_active=%s); skipping freshness checks", kill_active)
        return

    # 2. run.log freshness
    if run_log.exists():
        log_age = now - run_log.stat().st_mtime
        log_fresh = log_age < LOG_STALE_THRESHOLD_S
        if not log_fresh and state.log_fresh:
            maybe_alert(
                cfg, state, "log_stale",
                "ERROR",
                f"run.log untouched for {log_age/60:.0f} min (threshold {LOG_STALE_THRESHOLD_S/60:.0f} min) — bot may be hung"
            )
        elif log_fresh and not state.log_fresh:
            maybe_alert(cfg, state, "log_recovered", "INFO", "run.log writes resumed")
        state.log_fresh = log_fresh
    else:
        if state.log_fresh:
            maybe_alert(cfg, state, "log_missing", "ERROR", f"run.log not found at {run_log}")
            state.log_fresh = False

    # 3. journal.jsonl freshness
    if journal.exists():
        jrn_age = now - journal.stat().st_mtime
        jrn_fresh = jrn_age < JOURNAL_STALE_THRESHOLD_S
        if not jrn_fresh and state.journal_fresh:
            maybe_alert(
                cfg, state, "journal_stale",
                "ERROR",
                f"journal.jsonl untouched for {jrn_age/60:.0f} min — reconcile loop may have stalled"
            )
        elif jrn_fresh and not state.journal_fresh:
            maybe_alert(cfg, state, "journal_recovered", "INFO", "journal writes resumed")
        state.journal_fresh = jrn_fresh
    else:
        if state.journal_fresh:
            maybe_alert(cfg, state, "journal_missing", "ERROR", f"journal.jsonl not found at {journal}")
            state.journal_fresh = False

    # 4. Last reconcile event age
    reconcile_age = last_journal_event_age_s(journal, "reconcile")
    if reconcile_age is not None:
        reconcile_fresh = reconcile_age < RECONCILE_LAG_THRESHOLD_S
        if not reconcile_fresh and state.reconcile_fresh:
            maybe_alert(
                cfg, state, "reconcile_lag",
                "WARN",
                f"last reconcile event was {reconcile_age/60:.0f} min ago (threshold {RECONCILE_LAG_THRESHOLD_S/60:.0f} min)"
            )
        elif reconcile_fresh and not state.reconcile_fresh:
            maybe_alert(cfg, state, "reconcile_recovered", "INFO", "reconcile loop healthy again")
        state.reconcile_fresh = reconcile_fresh


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--bot-dir", default=DEFAULT_BOT_DIR)
    parser.add_argument("--interval", type=int, default=60)
    parser.add_argument("--once", action="store_true", help="Run one pass and exit (for testing)")
    parser.add_argument("--probe", action="store_true", help="Send a delivery probe to Telegram and exit")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    bot_dir = Path(args.bot_dir).resolve()
    env = _load_env(bot_dir / ".env")
    cfg = WatchdogConfig(
        bot_dir=bot_dir,
        interval_s=args.interval,
        webhook_url=env.get("ALERT_WEBHOOK_URL", ""),
        chat_id=env.get("TELEGRAM_CHAT_ID", ""),
    )

    if args.probe:
        ok = telegram_alert(cfg, "INFO", "watchdog delivery probe — if you see this, alerts work")
        print(f"probe sent: ok={ok}")
        return 0 if ok else 1

    log.info("watchdog starting bot_dir=%s interval=%ds", cfg.bot_dir, cfg.interval_s)

    # Probe on startup so we know alerts work
    if cfg.webhook_url and cfg.chat_id:
        ok = telegram_alert(cfg, "INFO", f"watchdog started (interval={cfg.interval_s}s, bot_dir={cfg.bot_dir.name})")
        if not ok:
            log.error("startup probe FAILED — alerts will not work. Continuing anyway, but fix Telegram config.")
    else:
        log.error("ALERT_WEBHOOK_URL or TELEGRAM_CHAT_ID missing in .env — running in log-only mode")

    state = WatchdogState()

    # Graceful shutdown
    shutdown = [False]
    def _stop(_signum: int, _frame: object) -> None:
        log.info("received signal, shutting down")
        shutdown[0] = True
    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)

    if args.once:
        check_once(cfg, state)
        return 0

    while not shutdown[0]:
        try:
            check_once(cfg, state)
        except Exception:
            log.exception("check_once raised — continuing")
        time.sleep(cfg.interval_s)

    log.info("watchdog stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
