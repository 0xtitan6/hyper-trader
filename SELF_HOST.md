# SELF_HOST.md — running hyper-trader on your own account

This is the fork-and-run guide. The [README](README.md) explains *what the system is*; this doc explains *what to do during your first week of running it on real money*.

If anything below contradicts the README, the README wins (it's regenerated from code; this is the operator's playbook).

## Audience

Someone who:
- Has a Hyperliquid account with USDC on perp
- Has read the README and run `just check` clean
- Is comfortable with a terminal, Python venvs, `.env` files
- Understands they're about to put real money on the line under autonomous control

If any of that doesn't fit, stop here and read the README first.

## Before you fund it

A 30-minute checklist before sending real money:

1. **Generate an agent wallet, not your main wallet.** HL UI → API → "Generate API wallet". The agent wallet's private key goes in `.env` (`HL_PRIVATE_KEY`), your main wallet's address goes in `HL_ACCOUNT_ADDRESS`. Agent wallets sign orders but cannot withdraw — leak blast radius is bounded to "trades you didn't authorize," not "drained funds."
2. **Set a webhook.** Slack incoming-webhook or a Telegram bot URL works. Without it, you find out about settlement, kill events, and exposure breaches by reading logs after the fact. Set `ALERT_WEBHOOK_URL` and `alert_min_level: warn` minimum.
3. **Run `--preflight` first.** It tells you whether the SDK can talk to HL, whether your wallet shows up, and whether HIP-4 outcomes are visible. If preflight fails, the live run will fail too.
4. **Run `dry_run: true` for at least one full leader-refresh cycle (10 min).** Watch the logs. Confirm leaders get discovered, fills get observed, intents get computed, but no orders submit.
5. **Decide your loss budget before flipping `dry_run: false`.** Whatever number is in `risk.max_daily_loss_usd` should be a number you'd be calm about losing in an afternoon. The bot won't know if your conviction wobbles mid-loss.

## Recommended starter config

If this is your first time running it on real funds:

```yaml
risk:
  dry_run: false
  max_total_exposure_usd: 60       # ~ $50-100 starter, scaled with account size
  max_daily_loss_usd: 25           # 30-50% of account, depending on appetite
  allowed_market_types: [perp]     # outcomes have a separate USDH funding step
  kill_switch_file: ./KILL

sizing:
  mode: proportional
  proportional_fraction: 0.05      # mirror at 5% of leader's size
  max_per_trade_usd: 15
  min_per_trade_usd: 10            # HL enforces $10 perp minimum

discovery:
  period: 7d
  top_n: 5
  min_trades: 50
  min_volume_usd: 5000
  min_pnl_usd: 100
  refresh_seconds: 600
  use_quality_filter: true         # see "Tuning the leader filter" below
```

Fund the perp account with the smallest amount that still respects `min_per_trade_usd: 10` after margin requirements. ~$50 is a reasonable smallest-meaningful start. Anything less and one bad fill triggers a margin call.

## First-day playbook

```
T+0min       just run                            # bot starts, preflight runs, leaders subscribe
T+0-2min     Verify in logs:                     #   "Selected N/M leaders"
                                                 #   "Subscribing to fills for leader 0x…"
                                                 #   "Following N leaders. Send SIGINT/SIGTERM to stop."
T+0-30min    Watch state/run.log + state/journal.jsonl
T+30-60min   First leader fill arrives           # log line: leader_fill ... intent_skipped|risk_check
T+1-4h       First own_fill if leader trades     # tail journal for "event":"own_fill"
T+24h        Daily reconcile:
              - Account value vs starting funds
              - Closed P&L (sum closed_pnl in own_fills)
              - Fee drag (sum fee in own_fills)
              - Number of intent_skipped vs orders submitted
```

Things you should *expect* to see in the logs (not panic about):

- `WS stale: no messages in 121s` followed by `WS connection recovered` — Hyperliquid's WebSocket disconnects roughly every 30 minutes. Backfill via REST catches anything missed. As long as recovery follows within ~30s, this is normal.
- `intent_skipped reason: filter` — leader traded on a market type your config blocks (e.g. they traded HIP-4 outcomes but you have `allowed_market_types: [perp]`). This is correct behavior.
- `risk_check ok: false reason: exposure_cap` — bot tried to mirror but you're at your `max_total_exposure_usd`. Increase the cap or wait for a position to close. Not a bug.
- `Quality filter rejected N candidates` — most of the HL leaderboard gets rejected by the quality filter. That's the point.

Things that warrant attention:

- `ERROR` lines that don't reference WebSocket. Read the message.
- An `intent_skipped` reason you don't recognize. Grep `src/mirror.py` for it.
- The bot stops emitting heartbeats. Check `pgrep -af "src.main"`.

## Tuning the leader filter

This is the most operator-impactful knob in the whole system, so it deserves its own section.

### What `use_quality_filter` does

The Liquidiction leaderboard ranks leaders by raw 7-day PnL. That's a *terrible* signal for copy-trade-ability:

- A leader with $113K PnL might have made it on one lucky position (no edge to copy)
- A leader with $20K PnL might be a sub-second scalper (we can't compete with their timing)
- A leader with $5K PnL might flip-flop directions (mirroring just bleeds fees)

`use_quality_filter: true` adds a second screen via `src/leader_score.py`: it pulls the candidate's last 7 days of fills directly from HL and computes:

- `time_between_fills_p50_s` — median seconds between consecutive fills. Low = scalper. High = swing trader.
- `realized_pnl_sharpe` — mean(closedPnl) / std(closedPnl). Higher = more consistent edge.
- `direction_consistency` — max(buys, sells) / total. Higher = more directional conviction. 0.5 = pure flip-flop.
- `max_drawdown_usd` — peak-to-trough on cumulative PnL.

### Default thresholds and how to interpret rejections

The defaults in `DiscoveryConfig` are conservative. Likely outputs at startup:

```
Selected 1/50 leaders (period=7d, quality_filter=True): 0x…(rank=39, pnl=$1646, …)
Quality filter rejected 40 candidates. First few:
  0x160398a0…(holding_p50=15s < 300s)
  0x64646ff8…(holding_p50=20s < 300s)
  0x7258d00a…(holding_p50=1s < 300s)
```

If you're getting 0 or 1 leaders, the filter is doing its job — most of the HL leaderboard is HFT scalpers. Three options:

1. **Wait.** Liquidiction refreshes its leaderboard daily. Different leaders surface.
2. **Loosen `min_holding_time_s`.** Drop from 300 → 60 to capture fast-but-real swing traders. Don't drop below 30 — that's where the scalpers live.
3. **Loosen `min_sharpe` to 0.0.** Will let in more low-edge leaders. Safer than dropping holding-time.

Don't drop `min_direction_consistency` below 0.55 — anyone closer to 0.5 is mathematically a coin-flipper.

### When the leaderboard is dominated by outcome traders

If your bot is running with `allowed_market_types: [perp]` and nearly every `leader_fill` becomes `intent_skipped: filter`, your leaders are HIP-4 outcome traders, not perp traders. Two paths:

1. Fund USDH on spot and add `outcome` to `allowed_market_types` — but be aware HIP-4 outcomes have thin books and a worse track record for mirroring. Read [docs/HIP4_GREEKS.md](docs/HIP4_GREEKS.md) before doing this.
2. Use a perp-bias score filter that re-computes leader metrics over perp fills only. Watch the project's PR list for `score_perp_only` / `min_perp_fraction` — these knobs filter for leaders whose *perp activity* meets quality, not their full portfolio.

## Operations: the killswitch and friends

The kill switch is your biggest safety net. It's a file path:

```bash
# Halt new orders immediately. Existing positions stay open.
touch ./KILL

# Resume.
rm ./KILL
```

**This does not close positions.** It only stops *new* orders. To actually exit a position you'd have to:

1. Use the HL UI to close it manually (your main wallet still has full authority — agent wallets are a subset, not a replacement)
2. Or wait for the leader to close, which triggers a mirror close

Other operations:

```bash
# Status check
pgrep -af "src.main"                              # is the bot alive?
tail -f state/run.log                              # live log
tail -f state/journal.jsonl | jq -r .              # live trade events

# Account snapshot (read-only, uses Info API)
.venv/bin/python -c "
from hyperliquid.info import Info
i = Info('https://api.hyperliquid.xyz', skip_ws=True)
us = i.user_state('YOUR_MAIN_WALLET_ADDRESS')
print('value:', us['marginSummary']['accountValue'])
for ap in us.get('assetPositions', []):
    p = ap['position']
    print(f\"  {p['coin']}: szi={p['szi']} unrealized={p['unrealizedPnl']}\")
"

# Daily reconcile: fees + closed P&L
grep '"event":"own_fill"' state/journal.jsonl | python3 -c "
import sys, json
fills = [json.loads(l) for l in sys.stdin]
print(f'fills: {len(fills)}')
print(f'fees:   \${sum(f[\"fee\"] for f in fills):.2f}')
print(f'closed: \${sum(f[\"closed_pnl\"] for f in fills):.2f}')
"
```

Plan to read these every day. The bot is autonomous; you're not.

## Common failure modes

| Symptom | Likely cause | Fix |
|---|---|---|
| Preflight fails with "outcome markets: 0" | HL has no live HIP-4 outcomes right now | Wait — HL launches new ones daily. Or set `allowed_market_types: [perp]` and ignore. |
| `KeyError: '#NN'` when placing an outcome order | `register_outcome_assets` didn't run on `exchange.info` | Already fixed in [`src/main.py`](src/main.py); update from main if you forked early. |
| Phantom `@230` short position appears | HL auto-converted USDH → USDC and the spot pair fill registered as a position | Already fixed — `_on_fill` skips `coin.startswith("@") or "/" in coin`. Update if forked early. |
| Outcome position survives after settlement | Settlement event missed or watcher died | `positions.reconcile_with_user_state()` runs every 5 min; phantom outcome positions get cleared on the next pass. If not, manually `unmark_tid_seen` the relevant tid. |
| `WS stale` alerts every 30 min | Normal — HL force-rotates connections | Ignore. Bump `ws_stale_threshold_s` from 120 → 240 if the alerts are too noisy. |
| Bot ignores leader fills | Risk policy mismatch (e.g. leaders trade outcomes, you're perp-only) | See "Tuning the leader filter" above. |
| Account value drifts down with no own_fills | Funding fees (perp positions) or fee drag from earlier fills | `grep '"closed_pnl"' state/journal.jsonl` for the actual P&L; account value includes fees + funding. |

## Where to take this

The system is designed to be a base for further strategies. Three reasonable places to take it from here:

1. **Different leader sources.** `src/liquidiction.py` is one implementation of "give me top traders." Subclass `LiquidictionClient` against another leaderboard (Hyperscale, Hypurr, your own discovery) — the rest of the bot doesn't care.
2. **Different mirror logic.** `src/mirror.py` does proportional/fixed sizing today. You could mirror with a delay (let leader prove sticky), with leverage adjustment per position size, or with portfolio risk parity. The interface is `on_leader_fill(leader, fill_data) -> None`.
3. **Different markets.** The `allowed_market_types` list is enforced in `src/mirror.py`. Adding "futures" or another HL product is a question of HL SDK support, not bot architecture.

For supervisor-level concerns (multi-account, alerting tiers, cross-bot orchestration), the long-term direction is to expose the read-only data surfaces (account state, journal, leader scoring) as MCP tools so an LLM operator can introspect without writing ad-hoc Python every shift. That work is queued.

## Other docs to read

- [README](README.md) — feature surface, install, file layout
- [AGENTS](AGENTS.md) — for coding agents editing this repo
- [TRADING_AGENT](TRADING_AGENT.md) — supervisor-agent playbook
- [WATCH](WATCH.md) — live-ops standing orders during open positions
- [RULES](RULES.md) — trading rules of engagement
- [docs/HIP4_GREEKS](docs/HIP4_GREEKS.md) — read before enabling outcome trading
