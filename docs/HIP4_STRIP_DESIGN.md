# HIP4_STRIP_DESIGN.md — Strip-construction module (design only)

**Status:** DESIGN ONLY. Not implemented. Build when HL launches multi-strike outcome ladders.

**Audience:** future agent or contributor implementing the strip module. This doc captures the math, API, and operational concerns so the build is one focused session, not a research project.

## Goal

Synthesize **vanilla-option-like exposure** from a basket of HIP-4 binary outcomes. A vanilla call payoff `(S_T − K)⁺` can be approximated by a strip of binary "S_T > K_i" contracts at multiple strikes K_i.

This unlocks:
- Real options on Hyperliquid (capped, but with normal Greeks)
- Multi-strike portfolios with predictable risk
- Cross-product hedges (perp + strip = delta-neutral structured product)

## Why this can't ship today

HL currently lists **only 15-min and 1-day BTC binaries with a SINGLE strike each**. There is no strike ladder. Without multiple simultaneous strikes for the same expiry, you cannot construct a strip.

**Build trigger:** when `outcomeMeta` returns ≥ 3 outcomes for the same `(underlying, expiry, period)` with different `targetPrice` values.

## Math (concise)

### Payoff approximation

A capped call from strike `K` to cap `K_max` decomposes as:

```
(S_T − K)⁺ − (S_T − K_max)⁺ ≈ ΔK · Σᵢ 1{S_T > K_i}    for K_i in [K, K_max), step ΔK
```

So a long capped call is **long the binary "S_T > K_i" at every strike in the ladder**, with notional `ΔK` per binary.

A long uncapped call (well, capped at the highest available strike) is the same with K_max → highest available.

### Pricing

Each binary's mid is its risk-neutral probability of settling YES. The strip price is the sum:

```
C_strip ≈ ΔK · Σᵢ p(K_i)
```

No vol model, no Black-Scholes inversion needed if HL provides the probability ladder via outcomeMeta + book prices.

### Greeks (key insight)

Single-binary Greeks are anomalous (see [`HIP4_GREEKS.md`](./HIP4_GREEKS.md)). When summed across a dense ladder, **the anomalies cancel** and aggregate Greeks converge to the vanilla call's Greeks:

```
Δ_strip(S) → Δ_call(K, S) − Δ_call(K_max, S)
Γ_strip(S) → Γ_call(K, S) − Γ_call(K_max, S)
ν_strip(S) → ν_call(K, S) − ν_call(K_max, S)
```

In the limit `K_max → ∞` and `ΔK → 0`, the strip behaves exactly like a vanilla call.

In practice, finite ladders give **capped Greeks** — vega and gamma are clipped beyond `K_max`. For most use cases this is fine because realized risk doesn't typically reach the cap.

## API sketch

```python
# src/strip.py  (proposed)

@dataclass
class Binary:
    coin: str          # e.g. "#22"
    strike: float      # e.g. 80_000
    side: int          # 0 = YES (S_T > K), 1 = NO

@dataclass
class StripSpec:
    """Defines a target vanilla-option-like exposure."""
    underlying: str    # "BTC"
    expiry_ts: int     # UTC unix
    option_type: str   # "call" | "put"
    strike: float      # K
    cap: float | None  # K_max; None means use highest available
    target_notional: float  # USD notional of the synthesized position

class StripBuilder:
    def __init__(self, info: InfoProto, market_meta: MarketMeta):
        self.info = info
        self.market_meta = market_meta

    def discover_ladder(self, underlying: str, expiry_ts: int) -> list[Binary]:
        """Query outcomeMeta + l2Book; return all binaries matching
        (underlying, expiry_ts), sorted by strike. Returns empty list if
        no ladder exists yet."""

    def construct(self, spec: StripSpec, ladder: list[Binary]) -> list[tuple[Binary, float]]:
        """Given a ladder, return list of (binary, shares) trades to execute
        the spec. Sizes are derived from ΔK and target_notional.

        Validates:
          - ladder spans [strike, cap]
          - ΔK is reasonable (warns if > 5% of strike)
          - capital required fits in account (queries USDH balance)
        """

    def estimate_price(self, trades: list[tuple[Binary, float]]) -> dict:
        """Sum probabilities × sizes → total premium. Returns:
          { 'premium_usd': float, 'max_payoff': float,
            'breakeven': float, 'leg_count': int }
        """

    def estimate_greeks(self, trades, spot: float, sigma: float) -> dict:
        """Aggregate Greeks. Δ, Γ, ν, Θ summed across legs."""

class StripExecutor:
    """Wraps Exchange to execute a strip atomically (best-effort).

    HL doesn't have native multi-leg orders; we submit each leg in parallel
    and reconcile partial fills."""
    def execute(self, trades: list[tuple[Binary, float]],
                tif: str = "Ioc") -> StripFillResult: ...
```

## Operational concerns

### 1. Atomic execution doesn't exist

HL doesn't expose multi-leg orders. We submit each binary leg independently. Risks:
- Partial fills → strip is unbalanced → unintended Greek exposure
- Price moves between submissions → some legs fill at worse prices
- One leg gets rejected (insufficient USDH, market halted, etc.) → strip is incomplete

Mitigations:
- Submit all legs in parallel via `bulk_orders` (single request, near-simultaneous)
- Use `Ioc` for taker strips, accept partial-fill outcome
- For makers (post-only), accept that the strip emerges over time as quotes get hit
- Have a `StripReconciler` that detects unbalanced strips and either completes or unwinds

### 2. Capital asymmetry — short strips need portfolio margin

Long strip: pay `ΔK · Σ p_i` upfront. For OTM strips, `Σ p_i << Σ (1−p_i)`.
Short strip: pay `ΔK · Σ (1−p_i)` upfront. For OTM strips this is HUGE.

**Without portfolio margin, two offsetting strips (e.g. short call spread + short put spread)
require collateral against each tail separately**, even though only one tail can realize.

Until HL adds portfolio margin for HIP-4, **this module should default to long-strip-only**.
Short strips require explicit operator override + significantly higher capital allowance.

### 3. Settlement chunkiness

At expiry, each binary resolves discretely. The strip's payoff is a staircase, not a smooth ramp:
- Pre-expiry: mark-to-market is smooth (probabilities move continuously)
- At expiry: each leg settles to $0 or $1; total payoff is `count × $1` where count = number of legs that finished ITM

For `ΔK = $1,000` and `K_max − K = $20,000`, the staircase has 20 steps. Most users won't notice. For tighter ladders (`ΔK = $100`), it's effectively continuous.

### 4. Coin notation translation

HL spot balances show outcomes as `+NN` (e.g. `+22`).
HL trading uses `#NN` (e.g. `#22`).
Strip module needs to handle both — same translation already in `src/positions.py:reconcile_with_user_state()`.

### 5. Composability with the maker

Once strips exist, a market-maker on individual binary strikes is naturally **also a strip maker** if the maker covers a ladder. Each filled leg moves the operator's strip-equivalent exposure by `ΔK`. The maker's inventory limit should be expressed in strip-equivalent Greek units, not raw share counts.

Future refactor: `OutcomeMaker` accepts a `Ladder` config that auto-rebalances inventory across multiple strikes to maintain a target Greek profile.

## Implementation steps (when triggered)

1. **`Binary` + `StripSpec` dataclasses** in `src/strip.py` (~50 lines)
2. **`StripBuilder.discover_ladder()`** — query `outcomeMeta`, group by `(underlying, expiry, period)`, parse strike from description (~80 lines)
3. **`StripBuilder.construct()`** — compute leg sizes given target notional (~60 lines)
4. **`StripBuilder.estimate_price()`** + **`estimate_greeks()`** — sum across legs (~80 lines)
5. **`StripExecutor.execute()`** via `Exchange.bulk_orders` — parallel submit + reconcile (~100 lines)
6. **`StripReconciler`** — detect unbalanced strips and either complete or unwind (~60 lines)
7. **Tests** — every API method, edge cases for partial fills, capital asymmetry, malformed ladders (~300 lines)
8. **CLI**: `python -m src.strip --underlying BTC --expiry ... --strike 80000 --cap 90000 --notional 100`

Total: ~700 lines new code + ~300 lines tests. ~2-3 day build given HL provides the ladder.

## Open questions (resolve before/during build)

1. **Does HL's `outcomeMeta` parse target prices reliably?** Current format: `"class:priceBinary|underlying:BTC|expiry:20260505-0600|targetPrice:79980|period:1d"`. We'd need a parser. If HL changes the description format, our parser breaks. Better: lobby HL for structured `targetPrice` field on `outcomeMeta`.

2. **What happens if a leg's binary is illiquid?** A strip with one missing/thin leg has incorrect Greeks. Detect via book depth + spread thresholds; refuse to construct strips with `>X%` of legs failing liquidity check.

3. **Settlement timing — do all binaries in a ladder settle simultaneously?** Per HL design, YES (all share the same `expiry_ts`). But edge cases exist if HL settles one and not another. PositionTracker's settlement detection (PR #5) handles per-binary settlement; strip-aware PnL reporting needs to aggregate.

4. **Portfolio margin?** If HL adds it for HIP-4, short strips become viable. Until then, long-only strips. Open question to ops: lobby HL?

5. **Greeks model — Black-Scholes or empirical?** BS gives clean formulas but assumes log-normal returns and constant vol. For BTC binaries near expiry, both assumptions break. Empirical Greeks (sum of observed binary mid-price sensitivities) are more accurate but require historical book data. Start with BS; refine later.

## See also

- [`HIP4_GREEKS.md`](./HIP4_GREEKS.md) — Greeks anomalies on single binaries (motivation for this module)
- [`UPSTREAM_HL_SDK_HIP4_PATCH.md`](./UPSTREAM_HL_SDK_HIP4_PATCH.md) — upstream Python SDK fix this work depends on
- `src/maker.py` — where the strip ladder eventually overlaps with maker inventory
- `src/positions.py` — settlement detection + `+NN` ↔ `#NN` translation
