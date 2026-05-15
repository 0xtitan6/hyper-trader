# CAPITAL_LADDER.md — when to scale, when to retreat

This document is the operator's discipline contract for capital decisions on hyper-trader. It exists because in the heat of a flat or losing day, "let's just fund another $500 to make it real" feels reasonable — and it's exactly when that feeling is loudest that the move is wrong.

The rule: **capital scales with validated P&L, not with frustration.**

## The ladder

| Stage | Account size | What this proves | Trigger to advance |
|---|---|---|---|
| **1 — Validation** | $50–$100 | The bot's plumbing works on real money | 20 closed mirror trades, **net positive realized P&L** over ≥ 2 weeks |
| **2 — Confirmation** | $250–$500 | Edge survives 5x more capital + market impact | 50 closed trades, Sharpe ≥ 0.5, max drawdown < 15% from peak |
| **3 — Scale** | $1,500–$2,500 | Strategy compounds reliably | 100 closed trades sustained, 4+ profitable weeks, no major regressions |
| **4 — Production** | $7,500–$10,000 | Income is meaningful (~$50–$100/day at 1% daily edge) | $50–$100/day average realized over 30 days |

The dollar ranges aren't precise — operator judgment within them. The triggers ARE precise. They are the gate.

## Why these gates

- **20 closed trades** is the smallest sample where Sharpe estimates start being meaningful. Below that, variance dominates signal.
- **Sharpe ≥ 0.5** filters out leaders/strategies that win sometimes but bleed on average. Below that, you're not earning the risk you're taking.
- **Max drawdown < 15%** says the strategy survives bad days without psychological pressure. Bigger drawdowns at small scale = catastrophic at production scale.
- **30 days at production scale** before declaring victory. Crypto has 7-day regimes; 30 days catches at least one regime change.

## Trigger to retreat

**If the account drops 30% from its peak**, halve capital and reassess strategy. No "let me try one more thing" at full size. Halving is non-negotiable; if the strategy is genuinely broken, halving prevents the second 30% drop.

**If we lose 3 consecutive Stages-1-style validation cycles**, mirror is structurally broken at our scale. Pivot strategy entirely (maker, funding-carry, manual + risk-manager). Don't keep pouring capital into a bot that hasn't proven edge.

## What scales, what doesn't

When advancing a stage, only these change:
- `max_total_exposure_usd` — proportional to account
- `max_per_trade_usd` — typically 20–25% of the cap
- `max_daily_loss_usd` — 30–50% of account (tighter % at smaller stages)
- USDC deposit on perp account

These DON'T change between stages:
- The bot's logic, filters, weights
- Per-leader weights (those auto-update from Sharpe)
- Quality filter thresholds

The bot is the same at $90 and $9000. The variable is size. If the bot works at $90, it works at $9000. The question we're answering at each stage is whether it works at all.

## Diversification rule

Starting at **Stage 2**, run multiple uncorrelated strategies in parallel:
- Mirror (copy-trader)
- Maker (HIP-4 outcome paired quoting, when activated)
- Funding-carry (manual or rule-based, on extreme APR)

One bot, three income streams. Single-strategy concentration is fine at Stage 1 (we're testing one thing), risky at Stage 3+.

## What "$100/day" actually requires

Honest math, kept here so we don't pretend otherwise:
- $100/day on a $10,000 account = 1.0% daily, ~30%/month, 365% APR
- Top funds earn 0.2%/day. 1.0%/day sustained is rare and large.
- Realistic expectation at Stage 4: **$30–$100/day average** with some weekly volatility
- $100/day _every_ day is fantasy. $100/day _on average_ is achievable with proven edge

## Discipline rules (what we promise the future-frustrated version of ourselves)

1. **No emotional top-ups.** "I'm sure it'll work this time" is the most expensive sentence in trading. Triggers to advance are objective; meet them or stay where we are.
2. **Document drawdowns** in the journal/logs alongside wins. Forgotten losses lead to misjudged Sharpe.
3. **One retreat per peak.** If we drop 30% from peak A, halve capital. We do not advance again until we've reclaimed peak A AND hit the next stage's trigger.
4. **24-hour cooldown on big decisions.** Any decision involving >$500 of capital change waits until the next day. Frustration peaks fastest; clarity follows.
5. **External validation before Stage 4.** At ~$10K account, the strategy should be reproducible — another operator should be able to fork the repo, run it, and see comparable results. If it only works for us, it's probably not edge, just luck.

## Status tracker

Update this section when stage transitions happen.

- **2026-05-08**: At Stage 1. Account ~$90. 9 closed mirror trades, net realized ~+$1.55 (PENDLE +$1.86, DOGE -$0.28, XMR -$0.13, HYPE -$1.11, TON -$1.62, etc.). Need 20+ trades over 2 weeks before considering Stage 2.

- **2026-05-15**: **Stage 1→2 operator override.** Capital topped up +$500 (account now ~$600). Stage 1 trigger was *not* met (~10 round-trips not 20; ~10 days not 14; realized −$5.87 not net positive on the period — though unrealized was +$9 on ZEC/TAO shorts and HL portfolio showed +33% organic over the 10-day window). Rationale for override: two leaders consistently calling shorts right, PR #25 hardened the multi-leader failure mode that produced the −$5.87, more capital generates information faster than waiting at $90. Mitigation: deploying caps at **half of Stage 2 max** (`max_total_exposure_usd: 300`, `max_daily_loss_usd: 120`, `max_per_trade_usd: 75`) rather than full Stage 2 ($450/$200/$120). Full Stage 2 caps unlock only after the original Stage 1 trigger is genuinely met (20 round-trips + sustained net positive realized + 2 weeks). Effectively: **deploy the capital, throttle the deployment, still earn the trigger we skipped.** Discipline rules #1 (no emotional top-ups — this was deliberate not emotional, but still skipped the gate) and #4 (24h cooldown on >$500 changes) noted as future-tightening candidates.

---

*The point of this doc is to be loud when the operator is quiet, and quiet when the operator is loud. When you're flat for a day and want to scale anyway — read this. When you're up 5% and want to bet bigger — read this. When you're down 15% and want to reset — read this.*
