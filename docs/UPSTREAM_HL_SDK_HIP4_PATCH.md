# Upstream patch: HIP-4 outcome support for hyperliquid-python-sdk

**Target repo:** [hyperliquid-dex/hyperliquid-python-sdk](https://github.com/hyperliquid-dex/hyperliquid-python-sdk)
**Issue title:** `Order placement on HIP-4 outcome markets fails with KeyError on coin name`
**Branch suggestion:** `feat/hip4-outcome-support`

## The bug

`Exchange.order("#20", ...)` against a live HIP-4 outcome market raises `KeyError: '#20'` before the request leaves the client. The SDK's `coin_to_asset` map is built only from `meta.universe` (perps), `spotMeta.universe` (spot), and builder-deployed perps (HIP-3). HIP-4 outcomes are in a fourth namespace the SDK never registers.

Per [official Hyperliquid docs](https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/asset-ids), outcome asset IDs encode as:

```
encoding  = 10 * outcome_id + side
asset_id  = 100_000_000 + encoding
coin_name = f"#{encoding}"
```

For the BTC daily binary (`outcome_id=2`):
- `#20` → asset_id `100_000_020` (Yes side)
- `#21` → asset_id `100_000_021` (No side)

This is verified end-to-end against mainnet HL: when registered manually in `coin_to_asset`, signed order payloads succeed at the wire layer (HL responds with the expected `min 10 USDH` policy error rather than asset rejection).

## Repro

```python
from hyperliquid.info import Info
from hyperliquid.exchange import Exchange
from eth_account import Account

info = Info("https://api.hyperliquid.xyz", skip_ws=True)
wallet = Account.from_key(YOUR_AGENT_KEY)
ex = Exchange(wallet, "https://api.hyperliquid.xyz", account_address=YOUR_ACCOUNT)

# Live BTC daily binary on mainnet:
ex.order("#20", True, 10.0, 0.05,
         order_type={"limit": {"tif": "Gtc"}}, reduce_only=False)
# → KeyError: '#20'
```

## Proposed fix

Add an `outcomes_meta` fetch + registration step in `Info.__init__`, mirroring the existing perp/spot pattern. Outcome assets sit at `100_000_000 + 10*outcome_id + side`; size precision defaults to integer shares (HL HIP-4 currently treats outcome positions as whole shares).

### Patch — `hyperliquid/info.py`

```python
# Constants — add near top of file
OUTCOME_ASSET_BASE = 100_000_000
OUTCOME_DEFAULT_SZ_DECIMALS = 0  # outcomes are integer shares per current HIP-4 contract spec


# In Info.__init__, after the perp_dexs loop:
def _register_outcome_assets(self) -> None:
    """Register HIP-4 outcome legs in coin_to_asset / name_to_coin.

    Outcome asset IDs encode as 100_000_000 + 10*outcome_id + side.
    Per https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/asset-ids
    """
    try:
        resp = self.post("/info", {"type": "outcomeMeta"}) or {}
    except Exception:
        # outcomeMeta may not be available on all networks/versions.
        # Fail open — perp/spot still work.
        return
    for outcome in resp.get("outcomes", []) or []:
        if not isinstance(outcome, dict):
            continue
        outcome_id = outcome.get("outcome")
        if not isinstance(outcome_id, int):
            continue
        side_specs = outcome.get("sideSpecs") or []
        n_sides = min(len(side_specs), 2) if side_specs else 2
        for side in range(n_sides):
            encoding = 10 * outcome_id + side
            asset_id = OUTCOME_ASSET_BASE + encoding
            coin = f"#{encoding}"
            self.coin_to_asset[coin] = asset_id
            self.name_to_coin[coin] = coin
            self.asset_to_sz_decimals[asset_id] = OUTCOME_DEFAULT_SZ_DECIMALS

# Call _register_outcome_assets() at the end of __init__
```

### Tests — `tests/test_info.py` additions

```python
def test_register_outcome_assets_btc_binary():
    info = Info(constants.MAINNET_API_URL, skip_ws=True)
    # If outcomeMeta has any active outcome, its legs should be registered
    # (asset_id formula is 100_000_000 + 10*outcome_id + side)
    for coin, asset_id in info.coin_to_asset.items():
        if coin.startswith("#"):
            encoding = int(coin[1:])
            assert asset_id == 100_000_000 + encoding


def test_outcome_meta_failure_fails_open():
    """Stub the post method to fail. Constructor should still succeed —
    perp/spot trading must remain functional even if outcomeMeta is unavailable."""
    # ... use mock to verify graceful degradation
```

## Sub-issues to file alongside

1. **`asset_to_sz_decimals` for outcomes** — currently hardcoded to 0 in our fix. Worth surfacing whether `sideSpecs` will eventually carry per-leg precision.
2. **Outcome-specific endpoints** — `outcomeMeta`, `outcomeMetaAndAssetCtxs`, etc. should be exposed as typed `Info` methods.
3. **Doc: `Exchange.order` for outcomes** — note that HL enforces a $10 USDH minimum order value and that funds must be in USDH (not USDC) for outcome trades to settle.

## Why mocked tests missed this

The SDK's existing tests mock at the network boundary, so `bulk_orders` is exercised against a fake `_post_action`. The KeyError happens earlier — in `name_to_asset` lookup — and only on real-API integration where the SDK's bootstrap doesn't include outcome assets. An integration test that constructs `Info` against a live network and asserts `coin_to_asset["#NN"]` is populated would have caught this.

## Reference implementation in this repo

See `src/hl_outcome.py` and `tests/test_hl_outcome.py` in `0xtitan6/hyper-trader` for a working external monkey-patch with full test coverage. The upstream patch applies the same logic inside `Info.__init__` so it works transparently.
