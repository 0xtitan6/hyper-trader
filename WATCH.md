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
- Outcome maker (`src.maker`) — built and tested, not deployed yet. Run only on markets with `spread_bps ≥ 30` (default floor) AND with USDH funded.

### Maker module quick-start

```bash
.venv/bin/python -m src.maker \
  --coin "#20" \
  --expiry 2026-05-06T06:00:00+00:00 \
  --min-spread-bps 30 \
  --quote-size 1 \
  --max-position 20 \
  --max-inventory-usd 5 \
  --dry-run                # remove for live
```

Watches the L2 book, posts paired bid/ask quotes (post-only `Alo`) inside the touch with inventory skew, cancels and replaces on mid moves ≥ `cancel_threshold_bps`. **Only quotes when spread ≥ floor — refuses negative-EV markets after fees.** Stops `expiry_buffer_s` (default 300s) before settlement.

**Reading the journal events:**
- `maker_quote` / `maker_quote_dry` — placed a quote
- `maker_skip reason=spread_too_tight` — book is tighter than the floor
- `maker_skip reason=bid_out_of_bounds` — sanity bound suppressed bid
- `maker_skip reason=quotes_crossed` — math produced bid≥ask, abstained
- `maker_fill` — our quote got hit (inventory updated)
- `maker_cancel_all` — cleared all resting orders

**Risk reference for the maker:** [`docs/HIP4_GREEKS.md`](./docs/HIP4_GREEKS.md) — single-binary Greeks are humped/sign-flipping, NOT vanilla-option-like. Read before lifting `max_inventory_usd`.

**Future direction:** [`docs/HIP4_STRIP_DESIGN.md`](./docs/HIP4_STRIP_DESIGN.md) — design doc for synthesizing vanilla-option-like exposure from binary strips. Build trigger: when HL launches multi-strike outcome ladders.

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

## Update log

- **15:50 UTC 2026-05-05** — Added `docs/HIP4_GREEKS.md` (Greeks reference) + `docs/HIP4_STRIP_DESIGN.md` (strip-construction architecture for when HL launches multi-strike ladders). WATCH.md links both under maker section.
- **14:10 UTC 2026-05-05** — Maker module shipped (PR #6 merged). Settlement detection + outcome reconcile fix merged (PR #5).
- **02:46 UTC 2026-05-05** — Created during active BTC binary trade. Position −$5.26, BTC $80,638, 3h 14m to expiry.
