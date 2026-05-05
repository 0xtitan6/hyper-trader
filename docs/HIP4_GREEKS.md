# HIP4_GREEKS.md — Binary outcome Greeks reference

**Audience:** anyone running the `OutcomeMaker` or sizing single-binary positions on HIP-4. Single binaries don't behave like vanilla options; their Greeks have anomalies that matter for risk management.

> Source for the math: derivation in the binary-options-vs-vanilla-options analysis Titan referenced, applied to HIP-4 binaries that resolve to $0 or $1 at expiry.

## TL;DR

A single HIP-4 binary is **not a vanilla option**. Its Greeks are humped and sign-flipping, not monotonic. Don't size single-binary inventory using vanilla-option intuition — you'll get the risk wrong.

The maker's hard caps (`max_inventory_usd=$5`, `max_position_shares=20`) and spread floor (`min_spread_bps=30`) are the safety net. **Don't relax them without re-deriving the risk.**

If you want vanilla-option-like exposure, build a **strip** of multiple binaries at different strikes (see [`HIP4_STRIP_DESIGN.md`](./HIP4_STRIP_DESIGN.md)). Greeks converge back to normal when summed across a dense ladder.

## Notation

- `S` — underlying price (e.g. BTC)
- `K` — binary's strike (e.g. $79,980)
- `τ` — time to expiry (years)
- `σ` — implied vol of the underlying
- `p(S)` — binary's mid price = risk-neutral probability of finishing ITM

## Anomalies vs vanilla calls

### Delta — humped, not sigmoid

Vanilla call: monotonic from 0 (deep OTM) to 1 (deep ITM).
**Binary: peaks near the strike, decays on both sides.**

```
Δ_binary = e^(-rτ) · φ(d₂) / (S · σ · √τ)
```

| Spot location | Δ_binary | Intuition |
|---|---|---|
| Deep OTM | ≈ 0 | Tiny moves don't change the probability |
| Near strike | maximum | A coin flip — every $1 shifts probability materially |
| Deep ITM | ≈ 0 | Already pinned to $1; further moves don't help |

**Operational implication:** when our inventory is in a binary whose strike is close to current spot, **we have maximum exposure to spot moves**. A single bad tick can move the position rapidly. Size accordingly.

### Gamma — sign-flips at the strike

Vanilla call: gamma is always positive (long convexity).
**Binary: gamma flips sign around the strike.**

```
Γ_binary = -e^(-rτ) · φ(d₂) · d₁ / (S² · σ² · τ)
```

| Spot location | Γ_binary | What it means |
|---|---|---|
| Below strike | positive | Long convexity — big up moves help |
| Above strike | **negative** | **Short convexity** — big down moves hurt |

**Negative gamma is the single most dangerous attribute** for an unhedged inventory holder. When you're long a binary that's already ITM, you're essentially short volatility — every reversal hurts disproportionately.

**Operational implication:** if the maker accumulates inventory in an ITM binary, it's holding short-gamma risk. The 5-min cancel-replace cycle helps because we're not trying to be a long-term holder — we just sit on the book briefly.

### Vega — sign-flips around the strike

Vanilla call: vega is always positive (more vol helps).
**Binary: vega depends on which side of the strike you're on.**

```
ν_binary = -e^(-rτ) · φ(d₂) · d₁ / σ
```

| Spot location | Vega | Intuition |
|---|---|---|
| OTM | positive | More vol = more chance of crossing strike |
| ITM | **negative** | More vol = more chance of falling back across strike |

**Operational implication:** holding ITM binaries means we're short vol. If realized vol spikes after we accumulate inventory, the position bleeds even if spot doesn't move.

### Theta — also sign-flips

Vanilla call: theta is always negative (long calls bleed).
**Binary: theta sign depends on which side of the strike you're on.**

| Spot location | Θ for long | What happens as time passes |
|---|---|---|
| OTM | negative | Less time to climb in → price decays toward $0 |
| ITM | **positive** | Less time to fall back out → price grinds toward $1 |

**This is the structural edge our endgame strategy aimed to capture** (`src/endgame.py`): buy ITM binaries near expiry to collect positive theta as the price converges to $1. The math works; it's gated by HL's $10 USDH min order and access to ITM binaries near expiry.

## Spread-floor as a side-effect risk filter

Our `min_spread_bps=30` floor in the maker isn't just a fee-EV check. **It also filters out the most adversely-selected zones**:

- **Near the strike**, spreads on a thin book WIDEN dramatically as MMs hedge against the gamma flip. If the spread is below our floor, it usually means market is at the gamma-flip zone OR vol is being pinned.
- **Deep OTM/ITM**, spreads are NARROW because Greeks are tame. We avoid these — there's no edge to capture.

Translation: **the maker is most likely to actually quote on liquid binaries away from the strike, where Greeks are tamer.** This is by accident, not design — but it's a real safety property.

## Inventory limits — why $5 is genuinely small

A vanilla-option intuition would say: "$5 of inventory is nothing, we can scale." With binaries that intuition is wrong, because:

1. **Position can flip from long-gamma to short-gamma** with a $1 BTC move (when underlying crosses strike)
2. **Settlement is binary** — no smooth liquidation; one day the position pays $0 OR $1, no in-between
3. **Vol regime shift** can wipe ITM inventory faster than perp inventory of equivalent size

Until we have **strip-based hedging** that converts these weird Greeks back to vanilla-like behavior, $5 is a sensible cap. Don't lift it to $50 just because perp positions of $50 feel small.

## Quick reference card

| If you... | ...you are exposed to... | ...most dangerous when... |
|---|---|---|
| Long an OTM binary | + delta, + gamma, + vega, − theta | Volatility crashes, spot stays OTM |
| Long an ITM binary | + delta, **− gamma, − vega**, + theta | **Spot crosses back below strike** |
| Long a binary near strike | + max delta, ≈0 gamma, ≈0 vega | **Any decisive directional move** |
| Short a binary | mirror of above | Mirror of above |

## See also

- [`HIP4_STRIP_DESIGN.md`](./HIP4_STRIP_DESIGN.md) — synthesizing vanilla-option-like exposure from binary strips
- `src/maker.py` — the OutcomeMaker that lives within these constraints today
- [HL HIP-4 official docs](https://hyperliquid.gitbook.io/hyperliquid-docs/hyperliquid-improvement-proposals-hips/hip-4-outcome-markets)
