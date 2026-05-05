# RULES.md — Trading concepts + rules of engagement for the supervising agent

**Audience:** any agent (LLM or human) operating `hyper-trader` strategies. These rules are non-negotiable unless the operator explicitly overrides in a specific instance with full understanding of the consequence.

> Written after a 5x-leverage runaway on XMR (2026-05-05) that came within 18% of a wipe-out. The rules below are the lessons paid for, codified.

## Hard rules (never violate, no agent override)

1. **Engage the kill switch at the first sign of unauthorized position growth.** If the bot fires more than 2 mirrors past a configured cap (exposure, position, daily loss), `touch KILL` immediately and notify the operator. Don't wait for the cap to "self-correct" — race conditions exist.
2. **Never average down on a losing position to recover losses.** Each trade is an independent decision. Loss-recovery psychology produces the worst trades.
3. **Never lift a hard cap (`max_total_exposure_usd`, `max_daily_loss_usd`, `max_per_trade_usd`) without explicit operator approval expressed for THIS run.** Standing approval doesn't carry over.
4. **If you cannot articulate the trade's thesis in one sentence, don't take it / don't let the bot take it.** "Leader is buying so we're buying" is a meta-thesis, not a thesis. Acceptable as a rule for copy-trading, but with extra discipline (rule 12).
5. **Stop trading and ping the operator if any single-asset position exceeds 30% of account value.** Concentration risk. Diversification matters even with leverage caps.
6. **Never run a strategy whose code path was last modified <24h ago without test-coverage proving the change.** No "ship and pray" on live capital.
7. **Never disable webhook alerting (`ALERT_WEBHOOK_URL`) during active positions.** Operator notification is the safety net of last resort.

## Soft rules (default behavior; explicit operator override allowed)

8. **Default `dry_run: true` after any major code change** until operator confirms live. The default after a runaway is "off, not on".
9. **Default to `min_per_trade_usd: 10`** to match HL's enforced floor — sub-$10 attempts get rejected by HL anyway, just spam the journal.
10. **Default `proportional_fraction: 0.05` or smaller** for copy-trading — we don't have an information advantage; sized small.
11. **Skip exotic perps unless operator approved** — the leader copy-trade on XMR (a privacy coin most operators don't track) was an information asymmetry against us.
12. **For copy-trading specifically: prefer mirroring on entries, not exits.** If leader is closing a position they built earlier, our mirror opens a fresh inverse — at fair odds, with no edge. Open-vs-close detection is on the roadmap; until shipped, treat closing-fill mirrors as ~zero-EV.

## Trading concepts you MUST understand before acting

If you can't explain these, ASK before submitting any order.

### Notional vs cost basis vs margin

- **Notional** = `|size| × price` — the dollar amount of underlying exposure. A 0.666 XMR long at $408 has notional $272.
- **Cost basis** = `|size| × entry_price` — what you paid for the position. Same as notional at entry; diverges with price.
- **Margin used** = collateral HL has locked against this position. With 5x leverage available, $54 margin can carry $272 notional.
- **`hyper-trader`'s `max_total_exposure_usd` is COST BASIS, not margin.** A $60 cap means $60 of cost basis. With leverage, the actual notional carried can be MUCH higher. **This is exactly the gap that almost wiped capital on XMR.**

### Leverage and liquidation

- HL perps offer up to 50x on majors, lower on alts (XMR caps lower). The bot doesn't set leverage explicitly; it inherits HL's default cross-margin behavior.
- **Liquidation occurs when adverse price move × notional exceeds account equity.** At 5x effective leverage, a 20% adverse move = liquidation. At 10x, 10% does it. At 20x, 5% does it.
- **HL's `withdrawable` field on `clearinghouseState` shows your free collateral.** If `withdrawable < $1`, you're at maximum leverage. **Treat that as a red alert.**

### Realized vs unrealized P&L

- **`realized_pnl_today()`** in our codebase deducts fees and is what `max_daily_loss_usd` checks against.
- **Unrealized loss does NOT trigger the daily-loss cap.** A 30% adverse move on a 5x position produces -$80 unrealized but $0 realized. The cap won't engage; only liquidation will.
- **A position that's deep underwater but not closed is still bleeding** — funding rates on perps, opportunity cost, mental tax. Realize loss when thesis is broken.

### Stablecoin classes (HL specifics)

- **USDC** — canonical. Used for perp trading.
- **USDH** — required for HIP-4 outcome trading. HL auto-dusts USDH back to USDC unless "Opt Out of Spot Dusting" is enabled in operator settings.
- **`@230`** — USDH/USDC spot pair. Use this to swap between them. Spot-pair fills are NOT positions (PositionTracker filters them).

### Settlement (HIP-4 only)

- Outcome contracts settle to $0 or $1 per share at expiry, based on the underlying event.
- Settlement fills come over WS as `dir: "Settlement"` with `px: 0.0` or `1.0` and the realized closed_pnl. PositionTracker special-cases these (PR #5).
- **Settlement is non-negotiable** — once an outcome resolves, your shares are gone (or worth $1 each). No early-close after expiry.

## Decision authority — what the agent can do alone

**Unilateral (no operator approval needed):**
- Engage `KILL` switch (defensive — bot can't grow positions further).
- Stop a runaway bot process (`pkill -9 -f "src.main"`) when capital is at risk.
- Restart a watcher monitor that died.
- Read state, query HL APIs, summarize.
- Ship documentation.

**Requires explicit operator approval:**
- Submit any order (open, close, manual hedge, etc.).
- Lift any cap (exposure, daily loss, per-trade, leverage).
- Re-enable a strategy after disabling it.
- Run a strategy that was modified in the last 24h.
- Make any persistent change to `.env` or `config.yaml` that affects live behavior.

**Gray zone — operator should re-approve hourly during active live trading:**
- Bot continues running with current config.
- Strategy continues mirroring with current limits.
- Re-confirmation prevents stale approval from carrying through to a different market regime.

## Specific kill triggers

Engage `touch KILL` automatically (no need to ask first) when ANY of these:

- ≥3 unauthorized fills past `max_total_exposure_usd` (race condition signature)
- Single position >30% of account value
- Single position notional >2× cost-basis cap (leverage runaway)
- 3+ `pipeline_error` events in 60 seconds
- 2+ `order_failed` of the same error class
- WS staleness exceeds threshold AND backfill returns 0 fills for >10 min
- Realized loss approaching 80% of `max_daily_loss_usd`
- ANY case where you'd struggle to explain to the operator what's happening

## After engaging kill switch

1. Notify operator within 5 seconds with the specific trigger.
2. Snapshot full account state (positions, balances, recent fills).
3. Kill the bot process (`pkill -9 -f "src.main"`) for clean state.
4. Wait for operator decision: cut, hold, or close-and-rebuild.
5. Don't re-enable until operator types "go live" or equivalent — and ALWAYS bump `dry_run: true` first.

## Mental discipline — recognize these patterns in yourself

- **Sunk cost** — "We're already in this much, might as well stay." NO. Each moment is a new decision.
- **Recency bias** — "The last 5 trades won, so this one will too." NO. Each trade has its own EV.
- **Authority bias** — "The leader's smart, so this trade must be good." MAYBE. Smart traders also lose trades. Each is independent.
- **Hope-as-strategy** — "If we just hold longer, it'll come back." NO. Hope isn't an exit plan.
- **Overconfidence after small wins** — "We figured this out, let's lift caps." NO. Variance, not skill.

## Lessons codified from past incidents

| Date | Incident | Lesson now baked in |
|---|---|---|
| 2026-05-04 night | BTC binary contrarian loss (-$10) | Single-binary directional bets without thesis = guessing. Endgame strategy adds disciplined structure (PR #6). |
| 2026-05-05 morning | Settlement notification missed | Bot itself emits critical alerts on settlement (PR #5) so phone gets pinged regardless of agent state. |
| 2026-05-05 noon | Spot-pair phantom positions | PositionTracker filters `@NNN` and `/` fills (PR #9). |
| 2026-05-05 afternoon | XMR 5x leverage runaway | Race condition in `_submit_lock` lets in-flight orders bypass exposure cap. Fix in design (in-flight tally). Until shipped: bot stays off on rapid-fire leaders. **Engaging kill switch at fill #4 instead of #7 would have saved $14 of unintended exposure.** |

## See also

- [WATCH.md](WATCH.md) — live-ops standing orders (read on every cold-start)
- [TRADING_AGENT.md](TRADING_AGENT.md) — supervisor playbook (status report template)
- [AGENTS.md](AGENTS.md) — coding-agent rules
- [docs/HIP4_GREEKS.md](docs/HIP4_GREEKS.md) — single-binary risk math
- [docs/HIP4_STRIP_DESIGN.md](docs/HIP4_STRIP_DESIGN.md) — future strip module
