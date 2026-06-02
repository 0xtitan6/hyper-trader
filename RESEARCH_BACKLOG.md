# RESEARCH_BACKLOG.md — strategy ideas to revisit at later stages

This file captures research threads surfaced during the early validation phase (Stage 1, 2026-05) that didn't ship as code at the time. Each entry has a **Stage gate** — the trigger from `CAPITAL_LADDER.md` that justifies revisiting.

The point of this document is the same as `CAPITAL_LADDER.md`: be loud about discipline when the operator is loud about ideas. Pivoting strategies because of an interesting paper is the most common way validated capital ladders get derailed. Add ideas here, come back when the data permits.

---

## Long-term system features

### Per-position stop-loss
**What:** Configurable percentage-based drawdown trigger per position. When unrealized loss exceeds `risk.position_stop_loss_pct` of cost-basis notional, submit reduce_only IOC to flatten. Default OFF; operator opts in.

**Why:** Discussed 3 times (2026-05-26, 2026-05-27, 2026-05-30). Live cost on PURR + LIT: open positions sit at −15-25% from entry for days while leaders average down. NEAR locked −$8 loss when leader exit finally fired at the bottom — a −10% stop would have cut it at ~−$3.

**Where:** New module `src/stop_loss.py` (periodic check) or integrate into the existing 5-min reconcile loop. Use `LeaderReconciler`'s on-chain leader-position fetch as defense — if leader is *flat*, close at stop instantly; if leader is still in, give them slack until stop trigger.

**Backtest first:** Extend `src/backtest.py` with `--stop-loss-pct` arg, replay last 30d at -5/-10/-15/-20% thresholds. Pick the one that improves net realized. Without this data, threshold choice is guessing.

**Stage gate:** Ship anytime — operator-controlled risk parameter, not a strategy change. Defaults OFF until backtest validates threshold.

**Engineering estimate:** ~2 hours including tests + backtest extension.

---

### MirrorTrader thesis hook (#42)
**What:** Consult the per-coin thesis cache from `MirrorTrader._build_intent` to apply AMPLIFY / MIRROR / VETO logic:
- BULL stance + leader BUY → MIRROR (or AMPLIFY × 1.3 at confidence ≥ 0.7)
- BULL + leader SELL → VETO
- BEAR + leader SELL → MIRROR (or AMPLIFY at confidence ≥ 0.7)
- BEAR + leader BUY → VETO
- NEUTRAL or no cache entry → MIRROR (no opinion)

**Why:** Layer 4 of the thesis stack started in PR #37/#38/#39. Without the hook, the cache populates but doesn't gate trades. This is what makes the thesis layer actually impact P&L.

**Where:** `src/mirror.py`, new config flag `mirror.thesis_filter_enabled: bool = False`.

**Stage gate:** Ship after generator output validates against live signals for 1-2 weeks. Default OFF.

**Engineering estimate:** ~1 day including tests + careful integration.

---

### Restart resilience — preflight retry on 429
**What:** When the bot's startup `Info()` constructor or preflight hits HL 429 rate limit, retry with backoff instead of crashing. Currently a single 429 kills startup (live cost 2026-05-29: 30 min downtime after a restart attempt during rapid PnL queries hammered HL).

**Where:** `src/main.py` startup section, wrap `Info()` construction in retry loop. Could also catch `ClientError(429)` and back off.

**Stage gate:** Ship anytime — pure resilience improvement, no behavior change in steady state.

**Engineering estimate:** ~1 hour.

---

### Funding-aware P&L reporting
**What:** Roll up `funding_events` table into all P&L reports. Currently `daily_pnl()` and lifetime queries only sum `closed_pnl - fee` from `own_fills`. Funding accrual is recorded but never surfaces.

**Why:** Closes the books gap between our DB-only realized number and HL portfolio view. At Stage 3+ scale funding becomes a real income stream worth reporting separately.

**Where:** Extend `state.daily_pnl` to optionally include funding, OR add new method `state.daily_pnl_with_funding`. Update operator queries.

**Stage gate:** Anytime.

**Engineering estimate:** ~2 hours.

---

### Account-value-aware sizing
**What:** Replace fixed `account_proxy` baseline in proportional sizing with live account-value lookup. Currently sizing is `fraction × static_base × weight` — as the account grows, sizing doesn't grow with it until operator manually bumps caps.

**Why:** Account has grown $590 → $670+ during Stage 1, but sizing logic still uses the same base. Compounding the strategy as it earns is structurally the right move.

**Where:** `src/mirror.py:_build_intent` — query `positions.total_account_value()` instead of hardcoded base.

**Stage gate:** Stage 2+ (need stable strategy first).

**Engineering estimate:** ~3 hours including tests.

---

### Operator PnL/status CLI
**What:** `python -m src.status` — one-shot CLI that prints current positions with mark-to-market unrealized, today's realized, account value, cap utilization. Replaces ad-hoc Python queries.

**Why:** Operator (or me from a fresh session) asks for PnL 10+ times/day. Each one is an ad-hoc Python script. A maintained tool is faster and less error-prone.

**Where:** New `src/status_cli.py`, similar pattern to `thesis_cli.py`.

**Stage gate:** Anytime — operator tooling.

**Engineering estimate:** ~2 hours.

---

### HL Vault deployment (endgame)
**What:** Deploy hyper-trader as a Hyperliquid vault contract. Other users deposit USDC, vault contract runs our strategy, depositors get pro-rata share of P&L minus 5% performance fee.

**Why:** Strategy capital efficiency × N depositors instead of just our own bag. Permissionless — no fund formation, no KYC for the manager, no LP agreements. Discussed 2026-05-27 (X tweet from college_xyz).

**Stage gate:** Stage 3+. Need 100 closed trades + 4 profitable weeks + max drawdown < 15% as the public-facing proof. Regulatory concerns (US-based operators) need review.

**Engineering estimate:** Probably 1-2 weeks. Need to wrap the bot's trading wallet in vault contract calls.

---

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

### Ika / ODWS for cross-chain agent signing
**What:** Ika is a Sui-based 2PC-MPC signing network. ODWS (Open dWallet Standard, by @iamknownasfesal) is its developer surface — a multi-chain wallet SDK where a single dWallet signs across EVM, BTC, Solana, Sui, Cosmos, TON, Tron, Filecoin without the agent ever holding a private key. Signing requires both user-side and network-side participation, with the network side gated by an on-chain Policy Engine (rate limits, spend budgets, venue/asset allowlists).

**Why it does NOT matter for hyper-trader (HL-only):** HL's agent wallet already gives us "constrained authority" at the venue layer — agent key can sign orders but cannot withdraw. Adding an MPC layer here is cargo-cult — more attack surface, no marginal safety. Our policy engine is `_risk_check` + capital ladder YAML, and that's appropriate for single-venue ops.

**Why it MIGHT matter for Stage 3+ cross-chain pods:** Once we sign on chains without a built-in agent-wallet primitive (Sui copytrade, Drift on Solana, BTC collateral movement, treasury sweeps across rails), one cryptographic policy surface beats N drifting Python risk gates that fall out of sync. Specifically relevant if/when we:
- Run a Sui-native copytrade pod
- Run Drift/Solana perps
- Move main-wallet capital between chains via automation (cold-storage sweeps, cross-chain rebalancing)
- Need BTC collateral exposure

**Stage gate:** Stage 3+ AND we're operating on ≥2 chains AND the spec is audited (currently "beta, unaudited" per author 2026-05-14). Until audited, evaluating it is premature — we're not betting agent capital on a beta MPC network even if the design is sound.

**Source:** @ikadotxyz thread + @iamknownasfesal ODWS preview, 2026-05-14.

**Engineering estimate:** N/A until audited + we have multi-chain need. At that point, ~1-2 weeks to integrate the SDK and define our policy schema, longer to harden.

---

## Discipline notes

When evaluating any of these for activation:

1. **Read `CAPITAL_LADDER.md` first.** The stage gates above are not negotiable just because something looks shiny.
2. **At least one validated existing strategy before adding a second.** Don't run mirror + bayes-weighting + ML-signal + pairs-trading simultaneously on $90.
3. **Diversification rule kicks in at Stage 2.** Until then, focus on making one thing work.
4. **24-hour cooldown on activation decisions.** If a paper makes you want to ship something tonight, that's the signal to wait. The stage trigger is the green light, not the inspiration.
5. **Platform-mortality filter (added 2026-05-11):** the prediction-market industry has ~90% mortality across 537 attempts per the public registry. Survivors are all crypto-native (Polymarket, Kalshi, Hyperliquid HIP-4) with ≥12 months of growing volume. **Rule:** cross-platform expansion requires the target venue to have ≥12 months of uptime + growing volume curve. Pre-launch announcements ("DeepBook Predict shipping soon", "Aftermath teasing perps", etc.) are watch-only — no engineering work until they survive a year and have visible volume.

---

*Last updated: 2026-05-30. Add new entries with stage gate, source, engineering estimate. When something gets implemented, move it to the Shipped section below.*

---

## Shipped — audit trail of what made it from backlog → production

Reverse-chronological. Each entry records the original backlog idea and the PR(s) that shipped it.

| Date | Idea | Shipped as | Live? |
|---|---|---|---|
| 2026-05-28 | Periodic HIP-3 dex re-registration | **PR #40** | ✅ on main |
| 2026-05-27 | PREF MCP fear/greed sentiment in thesis evidence | **PR #39** | 🟡 merged to feature branch; PR #41 forward-merges to main |
| 2026-05-25 | Rule-based thesis generator (funding + cross-leader concordance) | **PR #38** | 🟡 same as #39 |
| 2026-05-24 | ThesisCache scaffolding + operator CLI | **PR #37** | ✅ on main, cache table exists |
| 2026-05-23 | Leader-edge backtest tool | **PR #36** | ✅ on main, informed leader-drop config change |
| 2026-05-23 | HIP-3 perp DEX asset registration | **PR #35** | ✅ on main |
| 2026-05-22 | Funding-payment history tracking (state DB table + hourly poll) | **PR #34** | ✅ on main, +$0.94 lifetime captured |
| 2026-05-21 | WS silent-degrade rebuild + replay subscriptions | **PR #33** | ✅ on main, prevents the 5-day-stale-position bug at root |
| 2026-05-21 | `set_position_originator` race fix (INSERT-OR-UPDATE) | **PR #32** | ✅ on main |
| 2026-05-21 | 24/7 watchdog (systemd service, Telegram on state transitions) | **PR #31** | ✅ on main, caught the 30-min journal-stale incident 2026-05-29 |
| 2026-05-21 | LeaderReconciler — detect + close stale mirrors when leader exits | **PR #30** | ✅ on main, auto-close enabled |
| 2026-05-15 | Per-coin weight-priority conflict lock (originator address) | **PR #25** | ✅ on main, prevents multi-leader whipsaw |
| 2026-05-15 | Per-leader sizing weights (auto-Sharpe + explicit overrides) | **PR #18** | ✅ on main |
| 2026-05-15 | Funding-aware mirror sizing | **PR #19** | ✅ on main (opt-in via config) |
| 2026-05-08 | CAPITAL_LADDER.md discipline contract | **PR #20** | ✅ on main, actively consulted on cap raises |
| 2026-05-06 | Leader-quality scorer (catches scalpers + flip-floppers) | **PR #13/#14/#15** | ✅ on main, perp-bias filter live |
| 2026-05-05 | In-flight notional tally (closes exposure-cap race) | **PR #11** | ✅ on main |

### Partial / superseded

- **"Cross-leader concordance feature"** (original entry under quantitative strategies): partially absorbed into thesis generator's concordance rule (PR #38). Still listed as a separate entry because the original spec contemplated a richer signal (boost weight + size by convergence strength), not just BULL/BEAR/NEUTRAL.
- **"MCP read-only data layer"**: adjacent solution — PREF MCP was onboarded 2026-05-23 giving me catalog access from this Claude Code session. The original idea (wrap *our own* state as MCP servers for cross-agent access) is still unshipped.
