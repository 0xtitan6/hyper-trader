# WATCH.md — Standing orders for the supervising agent

**Audience:** any agent (human or LLM) supervising the live `hyper-trader` deployment. Read this every cold-start. Update it as state changes.

## Hard rule

**Never go silent during an open position.** If the watcher monitor stops emitting heartbeats for >7 minutes during an active trade, assume it died and restart it. Operator should never have to ask "how is the trade looking?" — the answer should be in their pocket already.

## Current run state — 2026-05-05 02:46 UTC

### Open position
| Field | Value |
|---|---|
| Coin | `#21` (BTC daily binary, NO leg) |
| Size | 33 shares |
| Cost basis | $0.31062 |
| Entry total | $10.25 USDH |
| OID | 411011928439 |
| Settlement | 2026-05-05 06:00:00 UTC |
| Win condition | BTC < $79,980 at expiry → 33 × $1 = $33 (+$22.75) |
| Loss condition | BTC ≥ $79,980 at expiry → 33 × $0 = $0 (−$10.25) |

### Account balances (mainnet, `0xE503186067…4A1a`)
| Account | Balance | Notes |
|---|---|---|
| Spot USDC | $54.73 | Auto-converted from USDH by HL spot dusting |
| Spot USDH | $0.0008 (dust) | Re-acquire if endgame strategy needs to fire |
| Spot `+21` | 33 shares | Our NO position |
| Perp account | $0.00 | Empty since funds moved to spot for outcome trading |

### Active processes
| What | Task ID | Polls | Pings on |
|---|---|---|---|
| Position watcher v3 | `bjqomykv5` | 2s | PnL ≥$1.50, prob ≥3pp, BTC ≥$200, expiry milestones, **5min heartbeat** |
| USDH balance watcher | `brclnk9jt` | 15s | USDH funded threshold cross, perp value Δ≥$1 |
| Journal monitor | `bcn79as28` | tail -F | order_result, order_failed, pipeline_error, kill_switch trips |

### Disabled (intentionally)
- Copy-bot (`src.main`) — leader's edge was on entries (we missed those); mirroring their close-trades opens fresh shorts at fair odds = ~zero EV after fees
- Endgame strategy (`src.endgame`) — would fail with "Insufficient spot USDH" given current balance

## Standing orders — what to do when

### On any market signal during an open position
1. Read it. Compute current unrealized P&L vs cost basis.
2. Reply within 5 seconds with: state delta, new unrealized, what changed.
3. Don't editorialize unless asked. Operator can read numbers.

### On `EXPIRY_30m` countdown
- Refresh full state snapshot (position, balances, current mids, BTC).
- Brief operator with PnL summary + what to expect at settlement.

### On `EXPIRY_5m` countdown
- Switch to second-by-second narration.
- Watch for "miracle reversal" pattern (BTC crossing target with <5min left + NO mid spiking).
- If miracle pattern detected and operator asks → confirm action then execute.

### On `SETTLEMENT`
- Within 5s: post final verdict, our P&L, what we did right/wrong.
- Update this WATCH.md with closing state.
- Save a project memory of the trade outcome.

### Operator says "cut"
Sell all open outcome positions IOC at best bid. Confirm fills within 10s.

```python
ex.order(coin, False, sz, best_bid, order_type={"limit":{"tif":"Ioc"}}, reduce_only=False)
```

### Operator says "double down" / "add"
**Push back once.** Adding to a losing position concentrates risk. If operator confirms, size the add ≤50% of original position, never exceeding daily-loss cap remaining.

### Operator says "how's it looking"
Even if nothing fired since last ping, give a fresh state pull. Don't say "no change" — operator wants confirmation you're awake.

## Failure modes to watch for

| Symptom | Likely cause | Action |
|---|---|---|
| No heartbeat for >7 min during active trade | Watcher process died (silent exit) | Restart watcher, apologize, add diagnostics |
| `Insufficient spot balance asset=…` on outcome order | USDH was dust-converted by HL | Re-swap USDC→USDH manually (operator UI), re-arm |
| `Order must have minimum value of 10 USDH` | Order notional < $10 | Bump size, retry |
| `KeyError: '#NN'` | New outcome listed, register_outcome_assets needs re-run | Restart bot or call register again |
| Reconcile zeros a `@230` "position" | Spot swap registering as phantom position | Harmless, ignore |

## Known HL gotchas (caught the hard way)

- **Outcomes settle in USDH, not USDC** (HL dust-converts non-canonical stables back to USDC after a timer)
- **Agent wallets cannot move funds** between perp/spot — only main wallet can. By design.
- **Exchange creates its own internal Info instance** — `register_outcome_assets()` must run on `info` AND `exchange.info`
- **Outcome asset ID = 100_000_000 + 10*outcome_id + side** (per HL docs, verified end-to-end)
- **HL HIP-4 minimum order value = $10 USDH**
- **Outcome share rounding = integer shares** (no fractional shares)

## Operator profile (Titan / OxTitan6)

- Terse, action-oriented. Says "do it" → execute the full chain without re-asking.
- Pushes for action; will accept honest "no edge here" if the math is shown.
- Wants downside-framed risk discussion, not optimism.
- Trusts but verifies — keep all changes in PRs, no surprise commits to main.
- Will explicitly approve protocol overrides; defer to that authority once given.

## Lessons learned (live-fire)

These cost real time/money/embarrassment to discover. Don't repeat.

1. **Watcher processes can exit silently.** Symptom: heartbeat stops with no error. Cause uncertain — possibly OOM, possibly killed by sibling pkill. Mitigation: 5-min heartbeat in every monitor; restart if silence >7 min during open position.
2. **Copy-trading on leader EXITS is structurally near-zero EV.** The leader's edge was at ENTRY (which we missed); their exits are at fair odds. Mirroring opens shorts at no edge, then pays fees. Add open-vs-close detection before re-enabling outcome mirroring at scale.
3. **Outcome trading requires USDH, not USDC.** And HL auto-dusts non-canonical stables back to USDC unless "Opt Out of Spot Dusting" is enabled in the user's HL settings. Operator must do this manually.
4. **`Exchange` creates its own internal `Info` instance.** `register_outcome_assets()` MUST run on both the standalone info AND `exchange.info`. Otherwise order placement fails with KeyError.
5. **Mocked tests don't catch real-API gaps.** The hyperliquid-python-sdk's order tests mock `_post_action`, so the missing HIP-4 coin map was invisible to its CI. Real-API integration tests would have caught it. Keep at least one test that hits live mainnet.
6. **HL HIP-4 asset ID = 100_000_000 + 10*outcome_id + side.** Not in the upstream Python SDK as of v0.23. Our `src/hl_outcome.py` patches both info instances. Upstream PR draft in `docs/UPSTREAM_HL_SDK_HIP4_PATCH.md`.
7. **Min outcome order value = $10 USDH.** Below this HL rejects with `"Order must have minimum value of 10 USDH"`. Bot's `outcome_min_per_trade_usd` config field exists for this — set it to 10.
8. **Agent wallets cannot move funds between perp/spot account classes.** By design. Operator must do `usd_class_transfer` in HL UI. Don't waste time trying to bypass it.
9. **`@230` is the USDH/USDC spot pair.** Currently ~1:1 with $0.0001 spread. Use this for stablecoin swaps.
10. **Outcome positions show as `+NN` in `spotClearinghouseState.balances`**, not in `clearinghouseState.assetPositions`. The bot's `PositionTracker.reconcile_with_user_state()` queries the perp side only — outcomes invisible to it. Reconciler will zero "@230" pseudo-positions from spot trades; harmless but noisy.

## Update log

- **02:54 UTC 2026-05-05** — Added Lessons Learned section after operator request to retain learnings across sessions.
- **02:46 UTC 2026-05-05** — Created during active BTC binary trade. Position −$5.26, BTC $80,638, 3h 14m to expiry.
