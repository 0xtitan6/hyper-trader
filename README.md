# hyper-trader

Production-grade Hyperliquid HIP-4 copy-trading bot. Discovers top prediction-market traders from [Liquidiction](https://liquidiction.xyz)'s public leaderboard, mirrors their fills via Hyperliquid's websocket API, and submits sized IOC orders against your account with persistent risk controls.

## How it works

```
  Liquidiction API ──> leader discovery (refreshed every 10 min)
                            │
                            ▼
  Hyperliquid WS  ──> FillFollower  ──> MirrorTrader ──> Exchange.order
        │                  │                  │
        │                  ├─> dedupe via   ├─> sizing (proportional/fixed)
        │                  │   sqlite tids  ├─> risk checks (kill switch,
        │                  │                │   exposure cap, daily-loss)
        ▼                  ▼                ▼
  PositionTracker (own fills) ──> realized PnL + exposure
                                          │
                                          ▼
                                     daily-loss kill
```

Every leader fill, intent, risk decision, and order outcome is appended to a JSONL trade journal for forensics. State persists across restarts (sqlite).

## Features

- **Leader discovery** from Liquidiction with configurable filters (period, min trades / volume / PnL).
- **Real-time mirroring** via `userFills` websocket subscriptions; deduped by `tid` in sqlite so restarts don't replay.
- **Own-fill tracking** maintains realized PnL and position book — wires the daily-loss kill switch.
- **Risk rails**: kill switch file, max exposure (cost-basis), max daily loss, max/min per-trade size, allowed market types.
- **Preflight gate** (`--preflight` or auto on startup): probes HL `meta`, `spotMeta`, `outcomeMeta`, `user_state`, `user_fills`, validates fill schema against required fields, lists active HIP-4 outcomes, refuses startup on failure.
- **Tick/lot rounding** via cached `MarketMeta` — sizes rounded to per-coin `szDecimals`, prices to 5 sig figs (HL rule). Outcomes default to integer shares.
- **WS staleness backfill**: when no messages arrive within `ws_stale_threshold_s`, alerts fire AND a REST sweep via `userFillsByTime` catches missed fills (deduped through state).
- **Network mismatch refusal**: bot refuses to start if `HL_NETWORK` env doesn't match `network.hyperliquid_env` in YAML.
- **WS health monitor** with stale-connection alerts.
- **Webhook alerts** (Slack/Discord-compatible) for: kill switch trips, daily loss cap, exposure cap, WS staleness, order failures, pipeline exceptions.
- **JSONL trade journal** for forensics and audit.
- **Structured JSON logging** option for log aggregators.
- **Graceful shutdown** on SIGINT/SIGTERM.

## Setup

### 1. Install

```bash
cd ~/hyper-trader
just setup            # creates .venv, installs runtime + dev deps
# or manually:
python3 -m venv .venv && .venv/bin/pip install -e '.[dev]'
```

### 2. Configure

```bash
cp .env.example .env
cp config.example.yaml config.yaml
```

Edit `.env`:

| Variable | Purpose |
|---|---|
| `HL_NETWORK` | `testnet` or `mainnet`. Must match `network.hyperliquid_env` in `config.yaml`. |
| `HL_ACCOUNT_ADDRESS` | Your main wallet address holding funds. |
| `HL_PRIVATE_KEY` | **Use an agent wallet, not your main key.** Approve via Hyperliquid UI → API → "Generate API wallet". |
| `ALERT_WEBHOOK_URL` | Optional. Slack/Discord-compatible incoming webhook. Empty disables. |

Edit `config.yaml`. Defaults are conservative (`dry_run: true`, $500 total cap, $100 daily-loss cap, $100 max per trade, 5 leaders).

### 3. Testnet funding

Get test funds at https://app.hyperliquid-testnet.xyz/drip. Requires a prior mainnet deposit from the same address as a sybil filter.

### 4. Run

```bash
just run                                 # full bot (runs preflight first, then trades)
.venv/bin/python -m src.main --preflight # preflight only — verify connectivity, schema, outcomes
.venv/bin/python -m src.main --skip-preflight  # bypass preflight gate (not recommended)
```

In dry-run mode, logs show intended trades but never submit:

```
[DRY] BUY #11 12.345678 @ 0.5400 notional=$6.67 leader=0x64646ff8d2 tid=8472183
```

When picks look reasonable, set `risk.dry_run: false` and run again.

## Tests, lint, type-check

```bash
just check            # runs lint, typecheck, test
just test             # pytest only
just test-cov         # with coverage report
just lint             # ruff check + format check
just fmt              # auto-fix lint and reformat
just typecheck        # mypy
```

Current state: **142 tests, 88% coverage** (94–100% on every business module; strict mypy clean; ruff clean). `main.py` integration wiring (36% covered by helpers) runs an infinite loop and is exercised end-to-end at runtime rather than by unit tests.

Pre-commit hooks (`.pre-commit-config.yaml`) run ruff lint + format, mypy, and security checks (`detect-private-key`, `check-merge-conflict`, etc.) on every commit.

Test layout:

| File | What it covers |
|---|---|
| `tests/test_config.py` | YAML/env loading, every validation negative path, immutability |
| `tests/test_liquidiction.py` | Leaderboard pagination, malformed rows, 5xx, address normalization |
| `tests/test_leaders.py` | Filter logic for trades/volume/PnL, top-N truncation |
| `tests/test_state.py` | Sqlite schema, atomic tid dedupe under threading, daily PnL aggregation, position storage |
| `tests/test_journal.py` | JSONL append, parent-dir creation, thread-safe writes, non-serializable fallback |
| `tests/test_alerts.py` | Webhook level filtering, post-failure swallowed, disabled-when-empty |
| `tests/test_connection.py` | Stale alert fires once, recovery alert, threshold validation |
| `tests/test_positions.py` | Long/short open/close/flip, avg-px math, dedupe, malformed fill handling, exposure |
| `tests/test_position_properties.py` | **Hypothesis property tests** — full-close zeros position, weighted-avg between bounds, signed-size sum invariant, idempotent dedupe, non-negative avg |
| `tests/test_follower.py` | Subscription dedupe, snapshot vs live fills, dedupe across messages, REST `backfill()` correctness, RPC failure handling |
| `tests/test_market_meta.py` | Per-coin `szDecimals` lookup, outcome integer rounding, 5-sig-fig price rule, load idempotency, upstream-failure error mapping |
| `tests/test_preflight.py` | Healthy/unhealthy reports, schema validation against required fields, error path per endpoint |
| `tests/test_log.py` | Plain vs JSON formatter, exception inclusion, idempotent setup |
| `tests/test_main.py` | Argparse defaults & flags, alerter selection, signal handler |
| `tests/test_mirror.py` | Sizing modes, market-type allowlist, every risk-check rejection path, slippage + tick rounding, order-failure alerting & propagation |
| `tests/test_integration.py` | End-to-end: discovery → subscribe → mirror, both dry-run and live |

## Safety

- `dry_run: true` by default. Flip only after watching dry-run logs on testnet.
- **Network mismatch** between `.env` and `config.yaml` aborts startup.
- **Kill switch**: `touch KILL` (or whatever path you set) to halt new orders. Existing positions stay open.
- **Hard caps** in YAML: `max_total_exposure_usd` (cost-basis), `max_daily_loss_usd`, `max_per_trade_usd`.
- **Agent wallet recommended** so a leaked key can't withdraw funds.

## Limits and known gaps

- **WS user-sub cap**: Hyperliquid allows max 10 unique users per IP for `userFills` subs — `discovery.top_n` is capped at 10 by `load_config`.
- **HIP-4 in the typed SDK**: outcome markets aren't formally exposed; the bot uses the `coin` string (e.g. `#11`) which the SDK's `Exchange.order()` accepts. Fill schema and `outcomeMeta` endpoint were validated against the live mainnet API; coin notation `#10` (Yes), `#11` (No) confirmed.
- **Exposure is cost-basis, not mark-to-market**: for fully-collateralized HIP-4 outcomes that's the actual max loss; for perps it under-counts what a liquidation could cost.
- **No backtest harness** in v1 — testnet is the validation environment.
- **No order idempotency via `cloid`**: a crash between submit and journal-write means restart-time reconciliation isn't possible. Fine for low-frequency outcome trading; would matter for HFT.

## File layout

| Path | Purpose |
|---|---|
| `src/main.py` | Entrypoint, argparse CLI (`--preflight`, `--skip-preflight`, `--config`), signal handling, leader-refresh loop |
| `src/config.py` | YAML + env loading, schema validation, immutable dataclasses |
| `src/state.py` | Sqlite persistence (dedupe tids, own fills, positions, daily PnL) |
| `src/journal.py` | Append-only JSONL trade journal |
| `src/liquidiction.py` | Liquidiction leaderboard HTTP client |
| `src/leaders.py` | Filter candidates → leader set |
| `src/follower.py` | Subscribe to leader fills, dedupe, dispatch, REST backfill |
| `src/positions.py` | Subscribe to own fills, realized PnL, position book, exposure |
| `src/mirror.py` | Sizing, risk checks, tick/lot rounding, order submission, journaling |
| `src/preflight.py` | HL connectivity probe + fill-schema validation + outcome listing |
| `src/market_meta.py` | Cached perp/spot `szDecimals` and 5-sig-fig price rounding |
| `src/connection.py` | WS staleness monitor + alerts + `on_stale` backfill hook |
| `src/alerts.py` | Webhook alerter + null alerter (level filtering, best-effort) |
| `src/log.py` | Plain or JSON logging setup |
| `src/protocols.py` | `InfoProto` and `ExchangeProto` Protocol types for HL SDK |
| `src/errors.py` | Custom exception hierarchy (`HyperTraderError` and subclasses) |
