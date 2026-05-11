# RESEARCH_BACKLOG.md — strategy ideas to revisit at later stages

This file captures research threads surfaced during the early validation phase (Stage 1, 2026-05) that didn't ship as code at the time. Each entry has a **Stage gate** — the trigger from `CAPITAL_LADDER.md` that justifies revisiting.

The point of this document is the same as `CAPITAL_LADDER.md`: be loud about discipline when the operator is loud about ideas. Pivoting strategies because of an interesting paper is the most common way validated capital ladders get derailed. Add ideas here, come back when the data permits.

---

## Long-term system features

### MCP read-only data layer
**What:** Wrap `hl-info`, `liquidiction`, and `hyper-trader-state` as MCP servers so an LLM operator (me, or Cowork) can query account state, leader scoring, and journal P&L without writing ad-hoc Python every shift. Read-only — no order placement.

**Why:** Faster operator queries ("how is xyz:CL doing?"), enables future supervisor agents, forces clean documentation of every data surface.

**Where:** New `mcp_servers/` directory. Three thin wrappers around existing modules.

**Stage gate:** Stage 2 (after capital is at $500+ and operator turnaround time matters).

**Engineering estimate:** ~1-2 days for all three.

---

### Auto-rotation on cap-blocked leader fires
**What:** When `risk_check` rejects on `exposure_cap`, automatically close a stale position to make room for the new mirror — but only when (a) new signal is from a leader weighted ≥ 1.5×, AND (b) the candidate-to-close's leader has zeroed their on-chain position, AND (c) we're outside a 10-min cooldown.

**Why:** 2026-05-06 17:30 UTC observation: rank 13 fired three RWA mirror signals in 30 seconds, all blocked by exposure cap. Cap binding is now common; rotation captures otherwise-missed flow.

**Where:** `src/mirror.py`'s `_risk_check`, plus a new `LeaderPositionTracker` polling each leader's on-chain positions every 30s.

**Stage gate:** Stage 2 (cap binds more often as leaders multiply).

**Engineering estimate:** ~150 lines + tests. Half-day.

---

## Quantitative strategies (from research articles)

### Bayesian leader weight updating
**What:** Treat the manually-set or Sharpe-derived leader weights as priors. After each closed mirror trade, update each leader's posterior weight using Bayes' rule on realized P&L. Self-tuning replacement for `use_sharpe_weighting`.

**Why:** Discussed 2026-05-06 with Titan as next iteration of PR #18. Mathematically clean, sample-efficient (works at 30+ trades vs full RL needing thousands), explainable.

**Where:** New `src/leader_weights_bayes.py`. Replaces or supplements `discover_leaders` weight assignment.

**Stage gate:** Stage 1 completion (≥30 closed trades to seed posteriors).

**Engineering estimate:** ~100 lines + tests. ~Half-day.

---

### Multi-armed bandit for leader selection
**What:** Instead of fixed `top_n` quality-filtered + `always_follow`, treat each candidate leader as an arm and use Thompson sampling or UCB to allocate WS-sub slots to the highest-expected-reward leaders. Rewards = realized P&L from their fills.

**Why:** Discussed 2026-05-06 with Titan as RL-adjacent simpler alternative. Sample-efficient, handles non-stationarity better than full RL.

**Where:** Replaces `src/leaders.py:discover_leaders`. Big change.

**Stage gate:** Stage 3 ($2.5K + 100 closed trades — needs the data volume).

**Engineering estimate:** ~3-5 days including A/B harness vs current selection.

---

### LSTM signal model on stationary feature set
**What:** Train an LSTM to predict next-period direction (binary classification) from stationary HL features: log returns, vol ratios, momentum normalized by vol, volume z-scores, funding rates, spread metrics.

**Why:** Roan's 2026-05-08 article (correctly) argues neural networks learn `E[Y|X]` via squared error, and price-prediction fails on non-stationarity. Done correctly with stationary features + walk-forward validation, can produce a 52-57% directional signal.

**Source:** Roan @RohOnChain article on quant-roadmap.

**Where:** New `src/ml_signal/` directory. Independent from mirror — would feed signal into `MirrorTrader` as a multiplier or filter, similar to funding-aware sizing.

**Caveat:** At $90 with 50bps round-trip costs, even a 54% directional model nets ~zero after fees. ML edge needs scale to express itself. Production-grade implementation is months not weekends (data infra, training pipeline, drift detection, retraining schedule).

**Stage gate:** Stage 3+ ($2.5K+ capital, where ML edge actually compounds).

**Engineering estimate:** 6-12 weeks for production-grade.

---

### "Cross-leader concordance" feature
**What:** For any leader fill on coin C, count how many OTHER quality leaders entered C within ±5 minutes. Use as a confidence signal that boosts weight or filters trades.

**Why:** A single quality leader entering BTC long is one signal. Three independent quality leaders converging on BTC long in 5 minutes is geometrically less likely to be noise. We have privileged access to this feature (multi-leader WS subs); other systematic traders don't easily.

**Source:** Original — answered Roan's "what feature would no one else use" question 2026-05-08.

**Where:** Lightweight: add a rolling window cache to `src/follower.py`. The mirror consults it when sizing.

**Stage gate:** Could ship at Stage 1 — small change. But waiting for Stage 2 (more leaders) makes the signal more reliable.

**Engineering estimate:** ~80 lines + tests. ~1 day.

---

## Quantitative strategies (from "151 Trading Strategies" paper, Kakushadze & Serur 2018)

The paper's directly-applicable subset for our surface:

### §3.8 Pairs trading
**What:** Trade the spread between correlated assets. Cointegration test → enter when spread > N std deviations from mean → exit on mean reversion.

**Where it fits:** BTC/ETH spread, BNB/SOL spread, paper-stock-perp vs NYSE basis (xyz:HOOD vs HOOD spot), USDH/USDC spot (already pegged but micro-arb).

**Stage gate:** Stage 2. Needs enough capital to size BOTH legs simultaneously.

**Engineering estimate:** New `src/pairs.py`. ~3-4 days for production.

---

### §3.9-3.10 Mean reversion (single + multi-cluster)
**What:** After extreme moves (>N std from rolling mean), fade the move. Single-cluster = one asset; multi-cluster = portfolio of asset groups.

**Where it fits:** Alts after volatility spikes (FARTCOIN, CHIP, STRK had +20-25% days this week). Particularly liquid on HL.

**Stage gate:** Stage 2. Needs more concurrent positions than $90 cap allows.

---

### §3.19 Market making
**What:** Post-only paired bid/ask quotes with inventory skew, capture spread + maker rebates.

**Where it fits:** **`src/maker.py` already implements this** for HIP-4 outcomes. Currently parked due to thin HIP-4 books and prior loss.

**Stage gate:** Stage 1 → 2. After mirror is validated, activate maker on 1-2 most-liquid HIP-4 binaries with USDH funding.

---

### §3.20 Alpha combos
**What:** Combine multiple alpha signals (momentum, mean-reversion, factor) into a single weighted signal. Allocates capital to whichever signal is hottest.

**Where it fits:** **The natural endpoint of our system.** Mirror + funding-carry + ML-signal + pairs-trade combined into one decision layer.

**Stage gate:** Stage 3 (need 3+ working strategies first; nothing to combo until then).

---

## Mathematical / payoff-design directions

### Orbital-style concentrated liquidity for HIP-4 strike ladders
**What:** Adapt Paradigm's Orbital sphere-AMM math (designed for stablecoin pools clustered near $1) to HIP-4 binary strike ladders (N legs summing to 1, mutually exclusive outcomes). Provides 15-150x capital efficiency on HIP-4 liquidity provision vs naïve paired quoting.

**Why:** Mathematically the SAME problem class — N assets bounded sum, current state clusters near a "fair point" (1/N for ladders, implied probability for binaries), tick concentration around that point.

**Source:** Paradigm Orbital paper (2025-06), shared 2026-05-07.

**Where:** New product. Not a modification of `maker.py` — sufficiently different math to be its own module.

**Stage gate:** Stage 4+ (this is a research-and-build project, not a tactical move).

**Engineering estimate:** Months. Real research → prototype → production.

---

### HIP-4 payoff classifier + routing
**What:** Currently we treat all `#NN` markets identically. A classifier would tag each market: type (binary/scalar/range/multi-leg), settlement source (price/rate/external), window (at-expiry/continuous), underlying. Different market types route to different strategies.

**Why:** Per Blockworks 2026-05-07 HIP-4 deep-dive, the primitive supports far more than binaries — scalar, range, multi-leg, custom payoffs. Treating them uniformly leaves edge on the table.

**Source:** Blockworks HIP-4 research note, 2026-05-07.

**Where:** New `src/hip4_classifier.py`. Used by `mirror.py` and (eventually) `maker.py` to pick strategy per market.

**Stage gate:** Stage 2-3. Becomes valuable once HL has > 10 active outcome markets of varied types.

**Engineering estimate:** ~100 lines for classification + parsing, more for routing logic. 1-2 days.

---

## Operator tooling

### Perplexity / Financial Datasets API integration for RWA context
**What:** When bot mirrors an `xyz:*` paper-perp trade, async-fetch recent news/fundamentals from Perplexity Finance Search or Financial Datasets MCP. Post context summary to Telegram alongside the trade.

**Why:** RWA leaders trade real underlyings (xyz:HOOD, xyz:CL, xyz:GOOGL). Operator awareness of "what's happening with HOOD today" makes intervention decisions sharper.

**Source:** Perplexity launch 2026-05-06, Financial Datasets MCP launch 2026-05-07.

**Where:** New `src/finance_context.py`. Async, non-blocking, called from `mirror.py` when `coin.startswith("xyz:")` or `coin.startswith("cash:")`.

**Cost:** ~$2-5/day at our expected volume.

**Stage gate:** Stage 2. RWA fill volume is too low at Stage 1 to justify the engineering.

**Engineering estimate:** ~50 lines + Telegram dispatch. ~Half-day.

---

## Reference architectures

### QuantAgent — multi-agent LLM trading framework
**What:** Academic multi-agent system (Stony Brook + CMU + UBC + Yale + Fudan, 2025; arXiv 2509.09995). Four specialized LangChain/LangGraph agents (Indicator → Pattern → Trend → Decision) that read OHLC data and output trade directives. Repo: <https://github.com/Y-Research-SBU/QuantAgent>.

**Why it's relevant as a reference:** The multi-agent decomposition pattern (specialized analysis agents + a synthesis/decision agent) is structurally what an eventual MCP-mediated supervisor over hyper-trader should look like. Useful template for HOW to structure the supervisor when we get there.

**Why it's NOT a direct code drop:**
- Doesn't execute trades — generates recommendations only.
- Uses yfinance, not HL tick feeds.
- 30-candlestick window is tiny for our timescale.
- LLM agents have 1-3s latency — incompatible with mirror's 50-200ms reflex-level response.
- Research prototype, no benchmarks or live validation in the repo.

**Where it'd fit (in our system):** A SUPERVISOR layer that runs every N minutes, evaluates whether recent bot trades fit broader context, surfaces concerns to the operator. Slow + deliberate, complementary to the fast bot. Composes with `RESEARCH_BACKLOG.md → MCP read-only data layer` — the MCP servers expose data; QuantAgent-style multi-agent decomposition is HOW to consume it.

**Stage gate:** Stage 3+. Needs the MCP layer first (Stage 2), and a strategy worth supervising (Stage 3+).

**Engineering estimate:** Adapt rather than fork. 1-2 weeks for a supervisor that uses our MCP servers + LangGraph agent decomposition for periodic strategy review.

---

## Cross-platform expansion

### Polymarket-MM activation
**What:** The `polymarket-mm/` repo (Go-based Avellaneda-Stoikov maker for Polymarket US sports markets). Already customized for our use; currently dormant.

**Source:** Polymarket "swisstony" report 2026-05-07 showed $40M volume / $361K PnL in 18 days for a sports MM bot.

**Stage gate:** Stage 2 + (a) hyper-trader closes ≥10 round-trip mirrors AND (b) cumulative realized P&L > $20.

**Engineering:** Different stack (Go), different venue (Polymarket US), separate KYC + funding. Half-day operational setup, no code changes.

---

### Synthesis.trade copytrade API for Polymarket
**What:** `api.synthesis.trade` is a unified API surface over Polymarket + Kalshi with first-class copytrade endpoints (`POST /polygon-copytrades`, etc.). Configure a copytrade subscription via API; their backend handles the WS subscriptions, leader fill detection, and order placement on Polymarket on our behalf.

**Why:** Shortcuts months of engineering vs building Polymarket leader-discovery + execution from scratch. They've already solved the per-platform plumbing.

**Source:** Discovered 2026-05-10 via the operator-facing UI at `synthesis.trade`.

**Where:** New Python module talking to `api.synthesis.trade`. Auth via API key. Different from `polymarket-mm` (which is sports MM, low-frequency direct CLOB).

**Stage gate:** Stage 2 + we've answered three open questions:
1. What does their fee/markup structure look like? <20bp tolerable, >50bp marginal.
2. Do they expose Polymarket leader-discovery (their docs mention Kalshi leaderboard but not Polymarket)?
3. Reliability — is their backend production-grade?

**Engineering estimate:** ~3-5 days for a working integration once questions answered.

**Important caveat:** they're an aggregator middleware. Latency-sensitive strategies (e.g., sports MM, our polymarket-mm) should go DIRECT to Polymarket CLOB, not through synthesis.trade. This entry is specifically for retail-cadence copytrade where their abstraction adds more value than its fee overhead.

---

### Ondo tokenized stocks on HL spot — basis trade opportunity
**What:** Ondo's tokenized stocks (HOOD, GOOGL, NVDA, etc., ~$1B AUM) bridging to Hyperliquid spot via LayerZero starting 2026-05-11. Meltfinance and Felix protocol are the first HyperEVM integrations.

**Why this matters:** Unlocks **basis trades** between Ondo-bridged spot tokens (e.g. Ondo-HOOD) and existing xyz:HOOD perp. Specifically:
- `Long Ondo-HOOD spot + Short xyz:HOOD perp` = neutral on HOOD price, capture funding rate differential
- If xyz:HOOD funding stays positive (typical for retail-heavy paper-perps), short earns funding while spot hedges price
- Pure carry trade, no directional risk, structurally positive EV when funding is positive

**Where it fits in our system:**
- **Hyper-trader (copy-trade):** zero code change needed. `market_meta` refreshes pick up new HL spot tokens automatically. If leaders trade them, bot mirrors.
- **Future maker.py / parked strategies:** basis trades are exactly what concentrated maker strategies excel at. Stage 3+ activation.

**Stage gate:**
- Stage 1+ awareness: monitor journal for leader fills on Ondo tokens (auto-picked-up by market_meta)
- Stage 3+ action: build a basis-trade strategy module that pairs Ondo-spot + xyz-perp legs with funding-rate-aware sizing

**Source:** 2026-05-11 announcement (Ondo + LayerZero + Meltfinance/Felix).

**Engineering estimate:** Hands-off until leaders trade them. Active strategy: 1-2 weeks engineering once funding data on Ondo tokens stabilizes.

---

## Discipline notes

When evaluating any of these for activation:

1. **Read `CAPITAL_LADDER.md` first.** The stage gates above are not negotiable just because something looks shiny.
2. **At least one validated existing strategy before adding a second.** Don't run mirror + bayes-weighting + ML-signal + pairs-trading simultaneously on $90.
3. **Diversification rule kicks in at Stage 2.** Until then, focus on making one thing work.
4. **24-hour cooldown on activation decisions.** If a paper makes you want to ship something tonight, that's the signal to wait. The stage trigger is the green light, not the inspiration.
5. **Platform-mortality filter (added 2026-05-11):** the prediction-market industry has ~90% mortality across 537 attempts per the public registry. Survivors are all crypto-native (Polymarket, Kalshi, Hyperliquid HIP-4) with ≥12 months of growing volume. **Rule:** cross-platform expansion requires the target venue to have ≥12 months of uptime + growing volume curve. Pre-launch announcements ("DeepBook Predict shipping soon", "Aftermath teasing perps", etc.) are watch-only — no engineering work until they survive a year and have visible volume.

---

*Last updated: 2026-05-08. Add new entries with stage gate, source, engineering estimate. When something gets implemented, move it to a "Shipped" section at the bottom rather than deleting (audit trail of what we considered).*
