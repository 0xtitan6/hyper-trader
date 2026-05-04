# AGENTS.md — Operator runbook for autonomous coding agents

Audience: an LLM coding agent (Claude Code, Cursor, etc.) doing work on this repo. Read this before making any change that touches `src/mirror.py`, `src/positions.py`, `src/config.py`, or anything in the order-submission path.

## What this project is

A copy-trading bot for Hyperliquid HIP-4 prediction-market outcomes. Real money is moved if `risk.dry_run: false` and a real key is in `.env`. Treat every change as if it ships to production.

## Setup (one-time)

```bash
just setup            # creates .venv, installs runtime + dev deps
just check            # ruff + mypy + pytest, must be green before any change lands
```

Cross-platform: `just` recipes auto-pick `.venv/Scripts` on Windows, `.venv/bin` on Unix.

## Secret handling — NEVER

- **Never print, log, journal, or commit `HL_PRIVATE_KEY` or any value derived from it.**
- Never paste private keys into agent transcripts, even masked.
- Never read `.env` and surface its contents in summaries.
- `.env`, `config.yaml`, `state/`, and `KILL` are in `.gitignore` — keep them there.
- `pre-commit` runs `detect-private-key` — don't disable it.

## Preflight (always)

Before any live run:

```bash
just preflight
```

Required to pass:
- `perp markets > 0`, `account reachable`, `fill schema OK`, no errors.
- If outcome markets are empty (e.g. between expirations), that's not necessarily fatal — but flag it.

## Dry-run gate

`risk.dry_run: true` is the default. Flip to `false` only with explicit user approval, after:
1. Preflight green.
2. At least one full leader fill mirrored in dry-run mode without errors in the journal.
3. `max_total_exposure_usd`, `max_daily_loss_usd`, `max_per_trade_usd` set to amounts the operator has explicitly accepted (no inferred defaults).

## Daily ops checklist (when agent is monitoring)

1. `tail -f state/journal.jsonl` — look for `pipeline_error`, `order_failed`, `daily_loss_cap`, `kill_switch`.
2. Check WS health — `WS stale` alerts mean we're missing fills. Backfill should auto-fire; verify with `Backfill: N new fills` log lines.
3. Inspect realized PnL — `realized_pnl_today()` is **net of fees**. If it trends down, scale back `proportional_fraction` or set tighter `max_per_trade_usd`.
4. Watch position book vs. leader behavior — outcome markets settle, after which positions should net to zero.

## NEVER-DO list

- Never `--skip-preflight` in a real run unless the operator explicitly tells you to (e.g. preflight is intermittently flaky and you've separately verified API health).
- Never bypass the kill switch or remove the `KILL` file without operator confirmation.
- Never commit changes that loosen risk caps as part of an unrelated change.
- Never change `discovery.top_n > 9` — Hyperliquid's WS allows 10 unique-user subs per IP, the bot uses 1 for own-fill tracking.
- Never add a code path that submits orders outside `MirrorTrader._submit` — risk checks, journaling, and `_submit_lock` live there for a reason.
- Never disable `reduce_only` logic for closing trades — it prevents accidental over-shoot when leaders close their own positions.
- Never add HFT-grade latency optimizations that skip the journal write (forensic record is non-negotiable).

## Code-edit conventions

- **Keep the modules small.** Each file has one job. If you find yourself adding a fifth concern to `mirror.py`, split it.
- **Strict mypy is enforced** (`disallow_untyped_defs`). Annotate every function, including private helpers.
- **No `Any` for the SDK surface** — use `InfoProto` / `ExchangeProto` from `src/protocols.py`.
- **Tests are mandatory** for new public functions. Negative tests for new validation.
- **Custom exceptions** for new failure modes — extend `HyperTraderError` in `src/errors.py`.
- **No new dependencies** without operator approval.
- **The journal is append-only and is the source of truth** for forensics. Write to it whenever you decide to act or skip an action.

## When in doubt

Ask the operator. The cost of pausing is low; the cost of an unintended live trade is funds.
