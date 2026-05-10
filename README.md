# hyper-trader

Production-grade Python toolkit for **Hyperliquid HIP-4 outcome markets** + perps. Includes:

- **Copy-trader** — discovers top traders from [Liquidiction](https://liquidiction.xyz) and mirrors their fills via HL websockets with risk caps + journaling.
- **HIP-4 outcome trading** — patches the upstream Python SDK to support placing orders on outcome markets (the SDK currently omits the asset-ID encoding for `#NN` coins; this repo unblocks them).
- **Outcome maker** (`src/maker.py`) — post-only paired bid/ask quoting with inventory skew, fee-aware spread floor, hard caps. Refuses negative-EV markets after fees.
- **Endgame strategy** (`src/endgame.py`) — opportunistic time-decay capture near binary expiry.
- **Settlement detection** — the bot itself emits critical webhook alerts on HIP-4 settlement, so phone notifications fire even with no agent supervising.

Every leader fill, intent, risk decision, and order outcome is appended to a JSONL trade journal for forensics. State persists across restarts (sqlite). 228 tests passing, ruff/mypy clean (strict).

## How copy-trading works

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
                                     daily-loss kill + settlement alerts
```

## Features

### Trading capabilities
- **Copy-trade leaders** on perps, spot, or HIP-4 outcomes (configurable `allowed_market_types`).
- **HIP-4 outcome support** — `src/hl_outcome.py` registers outcome asset IDs (`100_000_000 + 10·outcome_id + side`) into the SDK's `coin_to_asset` map at startup. Verified end-to-end against mainnet HL.
- **Per-market-type minimum** order size (`outcome_min_per_trade_usd`) — HL enforces $10 USDH min on HIP-4 orders; perps allow much smaller. Configure independently.
- **Outcome maker** — a standalone process that quotes both sides of a HIP-4 binary, captures spread on round-trips. Default 30 bps spread floor (refuses fee-negative markets).
- **Endgame** — buys the winning leg of a near-expiry binary when risk premium opens up. Defensible small +EV strategy.

### Risk + safety
- **Risk rails**: kill switch file, max exposure (cost-basis), max daily loss, max/min per-trade size, allowed market types, separate outcome minimum.
- **Reduce-only on closing trades**: when a leader's fill strictly shrinks an existing opposing position (and doesn't flip through zero), the order is submitted with `reduce_only=True` and bypasses the exposure cap. Flips through zero stay normal orders. Re-evaluated inside the submit lock against fresh state.
- **Per-coin weight-priority lock** (PR #25): when multiple leaders disagree on the same coin, the higher-weight leader wins. The leader who first opened a position on coin X "owns" it via a sqlite-persisted `originator_address`; opposite-direction signals from any other leader are skipped UNLESS that leader has strictly higher weight. Prevents the multi-leader whipsaw failure mode (caught 2026-05-10: two leaders took opposite TON sides within 30 min, costing -$1.85 in fees + slippage).
- **Net-of-fees daily-loss kill**: `realized_pnl_today()` deducts trading fees, so the daily-loss cap actually represents what the operator loses.
- **Settlement detection**: HIP-4 settlement fills (`dir: "Settlement"`, `px: 0.0` or `1.0`) are special-cased — position zeroed, P&L recorded, **critical webhook alert fires** so operator gets phoned regardless of agent state.
- **Outcome reconcile**: HIP-4 outcome positions live in `spotClearinghouseState.balances` as `+NN`, NOT in `assetPositions`. PositionTracker queries both and merges, translating `+NN` ↔ `#NN`.
- **Network mismatch refusal**: bot refuses to start if `HL_NETWORK` env doesn't match `network.hyperliquid_env` in YAML.
- **WS staleness backfill**: when no messages arrive within `ws_stale_threshold_s`, alerts fire AND a REST sweep via `userFillsByTime` catches missed fills (deduped through state).
- **Agent-wallet enforcement**: agent keys can place orders but cannot withdraw — leaks bounded to unwanted trades.

### Operations
- **Preflight gate** (`--preflight` or auto on startup): probes HL `meta`, `spotMeta`, `outcomeMeta`, `user_state`, `user_fills`, validates fill schema against required fields, lists active HIP-4 outcomes, refuses startup on failure.
- **Tick/lot rounding** via cached `MarketMeta` — sizes rounded to per-coin `szDecimals`, prices to 5 sig figs (HL rule). Outcomes default to integer shares. `min_per_trade_usd` re-checked *after* rounding so sub-lot trades aren't submitted.
- **Configurable IOC slippage** (`sizing.ioc_slippage_bps`, default 50 bps = 0.5%, range `[0, 1000]`).
- **Async webhook delivery**: alerts dispatched to a daemon worker via a bounded queue (256). A slow webhook host can never block the WS handler thread; oversaturation drops the alert with a warning.
- **Webhook alerts** (Slack/Discord-compatible) for: kill switch, daily loss cap, exposure cap, WS staleness, order failures, pipeline exceptions, **settlement**.
- **JSONL trade journal** for forensics.
- **Structured JSON logging** option for log aggregators.
- **Graceful shutdown** on SIGINT/SIGTERM.

## Setup

### 1. Install

```bash
cd ~/hyper-trader
just setup            # creates .venv, installs runtime + dev deps
```

`just` recipes are cross-platform — they pick `.venv/Scripts/...` on Windows, `.venv/bin/...` on Unix. Manual install:

```bash
# Unix
python3 -m venv .venv && .venv/bin/pip install -e '.[dev]'
# Windows
python -m venv .venv && .venv\Scripts\pip install -e ".[dev]"
```

Requires Python **3.13+** (per `pyproject.toml`).

### 2. Configure

```bash
cp .env.example .env
cp config.example.yaml config.yaml
```

Edit `.env`:

| Variable | Purpose |
|---|---|
| `HL_NETWORK` | `testnet` or `mainnet`. Must match `network.hyperliquid_env` in `config.yaml`. |
| `HL_ACCOUNT_ADDRESS` | Your **main wallet** address (the one that holds funds — not the agent). |
| `HL_PRIVATE_KEY` | **Agent wallet's** private key. Approve via Hyperliquid UI → API → "Generate API wallet". Cannot withdraw. |
| `ALERT_WEBHOOK_URL` | **Strongly recommended for HIP-4 trading.** Slack/Discord-compatible incoming webhook — settlement alerts fire here. |

Edit `config.yaml`. Defaults are conservative (`dry_run: true`, $500 total cap, $100 daily-loss cap, $100 max per trade, 5 leaders).

### 3. Funding for HIP-4

HIP-4 outcomes settle in **USDH**, not USDC. To trade outcomes:

1. Disable HL's "Spot Dusting" auto-conversion in your account settings (otherwise USDH gets converted back to USDC).
2. Transfer USDC from perp → spot (HL UI requires main wallet signature; agent cannot do this).
3. Swap USDC → USDH on the `@230` spot pair (~1:1 pegged).
4. Outcome trades will succeed once `spotClearinghouseState.balances[USDH] >= 10`.

For testnet drips: <https://app.hyperliquid-testnet.xyz/drip> (requires a prior mainnet deposit from the same address as a sybil filter).

### 4. Run

```bash
# Full copy-bot (preflight first, then trade)
just run
.venv/bin/python -m src.main --preflight       # preflight only
.venv/bin/python -m src.main --skip-preflight  # bypass gate (not recommended)

# Outcome maker (separate process, one outcome leg)
.venv/bin/python -m src.maker --coin "#20" \
  --expiry 2026-05-06T06:00:00+00:00 \
  --min-spread-bps 30 --quote-size 1 \
  --max-position 20 --max-inventory-usd 5 \
  --dry-run                                   # remove for live

# Endgame strategy (time-decay capture near binary expiry)
.venv/bin/python -m src.endgame \
  --target 79980 --expiry 2026-05-06T06:00:00+00:00
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

Current state: **228 tests passing, ruff/mypy clean** with strict mypy. `main.py` integration wiring runs an infinite loop and is exercised end-to-end at runtime rather than by unit tests.

Pre-commit hooks (`.pre-commit-config.yaml`) run ruff lint + format, mypy, and security checks (`detect-private-key`, `check-merge-conflict`, etc.) on every commit.

Test layout:

| File | What it covers |
|---|---|
| `tests/test_config.py` | YAML/env loading, every validation negative path, immutability, `outcome_min_per_trade_usd` rules |
| `tests/test_liquidiction.py` | Leaderboard pagination, malformed rows, 5xx, address normalization |
| `tests/test_leaders.py` | Filter logic for trades/volume/PnL, top-N truncation |
| `tests/test_state.py` | Sqlite schema, atomic tid dedupe under threading, daily PnL aggregation, position storage, `unmark_tid_seen` |
| `tests/test_journal.py` | JSONL append, parent-dir creation, thread-safe writes, non-serializable fallback |
| `tests/test_alerts.py` | Webhook level filtering, post-failure swallowed, disabled-when-empty |
| `tests/test_connection.py` | Stale alert fires once, recovery alert, threshold validation |
| `tests/test_positions.py` | Long/short open/close/flip, avg-px math, dedupe, malformed fills, exposure, **outcome reconcile from spot**, **settlement detection + critical alert + journaling** |
| `tests/test_position_properties.py` | **Hypothesis property tests** — full-close zeros position, weighted-avg between bounds, signed-size sum invariant, idempotent dedupe, non-negative avg |
| `tests/test_follower.py` | Subscription dedupe, snapshot vs live fills, dedupe across messages, REST `backfill()` correctness, RPC failure handling |
| `tests/test_market_meta.py` | Per-coin `szDecimals` lookup, outcome integer rounding, 5-sig-fig price rule, load idempotency, upstream-failure error mapping |
| `tests/test_preflight.py` | Healthy/unhealthy reports, schema validation against required fields, error path per endpoint |
| `tests/test_log.py` | Plain vs JSON formatter, exception inclusion, idempotent setup |
| `tests/test_main.py` | Argparse defaults & flags, alerter selection, signal handler |
| `tests/test_mirror.py` | Sizing modes, market-type allowlist, every risk-check rejection path, slippage + tick rounding, order-failure alerting & propagation, **outcome-min override**, **pipeline-error tid retry** |
| `tests/test_integration.py` | End-to-end: discovery → subscribe → mirror, both dry-run and live |
| `tests/test_hl_outcome.py` | HIP-4 asset-ID encoding, outcome registration, idempotency, malformed responses |
| `tests/test_endgame.py` | Tier logic, dry-run, sanity bounds, kill switch, error propagation |
| `tests/test_maker.py` | Spread floor, inventory caps, post-only contract, sanity bounds, fill handling, cancel-replace, kill switch, expiry buffer |

## Safety

- `dry_run: true` by default. Flip only after watching dry-run logs on testnet.
- **Network mismatch** between `.env` and `config.yaml` aborts startup.
- **Kill switch**: `touch KILL` (or whatever path you set) to halt new orders. Existing positions stay open. Maker and endgame respect it too.
- **Hard caps** in YAML: `max_total_exposure_usd` (cost-basis), `max_daily_loss_usd`, `max_per_trade_usd`, `outcome_min_per_trade_usd`.
- **Agent wallet required** for live trading — agent keys cannot withdraw funds.
- **Webhook strongly recommended** for HIP-4 trading — settlement alerts fire critical-level pings to your phone direct, no agent supervision required.

## Documentation

For LLM agents driving the bot or contributors landing changes:

- **[AGENTS.md](AGENTS.md)** — operator runbook for autonomous *coding* agents (e.g. Cursor, Claude Code editing this repo). Setup, secret-handling rules, NEVER-DO list, code-edit conventions.
- **[TRADING_AGENT.md](TRADING_AGENT.md)** — trading-supervisor playbook for an LLM that *watches* a running bot. Inputs, dry-run → live decision tree, daily P&L tiered response, kill-switch protocol, status report template.
- **[WATCH.md](WATCH.md)** — live-ops standing orders. The agent supervising an active position reads this every cold-start. Includes failure-mode table, hard rules ("never go silent during open position"), HL gotchas list, maker quick-start.
- **[docs/HIP4_GREEKS.md](docs/HIP4_GREEKS.md)** — single-binary Greeks reference. Why binary delta is humped (not sigmoid), why gamma/vega/theta sign-flip around the strike, why `max_inventory_usd=$5` is genuinely small.
- **[docs/HIP4_STRIP_DESIGN.md](docs/HIP4_STRIP_DESIGN.md)** — design doc (not implementation) for synthesizing vanilla-option-like exposure from binary strips. Build trigger: HL launches multi-strike outcome ladders.
- **[docs/UPSTREAM_HL_SDK_HIP4_PATCH.md](docs/UPSTREAM_HL_SDK_HIP4_PATCH.md)** — ready-to-file upstream PR for `hyperliquid-dex/hyperliquid-python-sdk` adding native HIP-4 support.

## Limits and known gaps

- **WS user-sub cap**: Hyperliquid allows max 10 unique users per IP for `userFills` subs and the bot's own-fill subscription consumes 1 — `discovery.top_n` is capped at **9** by `load_config`.
- **Upstream Python SDK doesn't expose HIP-4** — handled here via `src/hl_outcome.py:register_outcome_assets()` patching `coin_to_asset` and `name_to_coin` at startup. Both `info` AND `exchange.info` need patching (Exchange spawns its own internal Info instance). Upstream PR draft in `docs/UPSTREAM_HL_SDK_HIP4_PATCH.md`.
- **Outcomes settle in USDH, not USDC** — operator must do USDC → USDH spot swap manually (HL agent wallets cannot move funds between perp/spot account classes).
- **HL Spot Dusting auto-converts USDH → USDC** unless disabled in HL UI settings — operator action required for sustained outcome trading.
- **Single-binary Greeks are non-vanilla** — humped delta, sign-flipping gamma/vega/theta. Don't size single-binary inventory using vanilla-option intuition. See [`docs/HIP4_GREEKS.md`](docs/HIP4_GREEKS.md).
- **Exposure is cost-basis, not mark-to-market**: for fully-collateralized HIP-4 outcomes that's the actual max loss; for perps it under-counts what a liquidation could cost.
- **Copy-bot mirroring has a structural EV gap on outcome closes** — a leader's edge is in their entries (which we missed by the time we subscribe); their exits are at fair odds. Mirroring leader sells without inventory opens fresh shorts at ~zero EV. Open-vs-close detection is a known follow-up.
- **Maker uses REST polling, not WebSocket book updates** — refresh interval default 2s. Lower-latency WS-driven version is a follow-up.
- **No backtest harness** in v1 — testnet is the validation environment.
- **No order idempotency via `cloid`** — a crash between submit and journal-write means restart-time reconciliation isn't possible. Fine for low-frequency outcome trading; would matter for HFT.
- **No portfolio margin awareness** — short-strip strategies (when they become buildable) require operator-managed capital reservations.

## File layout

| Path | Purpose |
|---|---|
| `src/main.py` | Copy-bot entrypoint, argparse CLI, signal handling, leader-refresh loop, periodic position reconcile |
| `src/maker.py` | HIP-4 outcome market-maker (post-only paired quotes with inventory skew + spread floor) |
| `src/endgame.py` | Time-decay capture strategy near binary expiry |
| `src/hl_outcome.py` | HIP-4 asset-ID registration patching `Info.coin_to_asset` |
| `src/config.py` | YAML + env loading, schema validation, immutable dataclasses (`outcome_min_per_trade_usd`) |
| `src/state.py` | Sqlite persistence (dedupe tids, own fills, positions, daily PnL, `unmark_tid_seen`) |
| `src/journal.py` | Append-only JSONL trade journal |
| `src/liquidiction.py` | Liquidiction leaderboard HTTP client |
| `src/leaders.py` | Filter candidates → leader set |
| `src/follower.py` | Subscribe to leader fills, dedupe, dispatch, REST backfill |
| `src/positions.py` | Subscribe to own fills, realized PnL, position book, exposure, settlement detection, outcome reconcile from spot |
| `src/mirror.py` | Sizing, risk checks, tick/lot rounding, order submission, journaling, per-market-type minima, pipeline-error tid retry |
| `src/preflight.py` | HL connectivity probe + fill-schema validation + outcome listing |
| `src/market_meta.py` | Cached perp/spot `szDecimals` and 5-sig-fig price rounding |
| `src/connection.py` | WS staleness monitor + alerts + `on_stale` backfill hook |
| `src/alerts.py` | Webhook alerter + null alerter (level filtering, best-effort, async queue) |
| `src/log.py` | Plain or JSON logging setup |
| `src/protocols.py` | `InfoProto` and `ExchangeProto` (incl. `cancel`) Protocol types for HL SDK |
| `src/errors.py` | Custom exception hierarchy (`HyperTraderError` and subclasses) |
