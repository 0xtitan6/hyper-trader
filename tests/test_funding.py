"""Tests for src/funding.py — FundingTracker (perp funding-rate cache)."""

from __future__ import annotations

from unittest.mock import MagicMock

from src.funding import FundingTracker


def _ctx(funding_hourly: float) -> dict:
    """Build a minimal asset-ctx with the funding field set (hourly rate)."""
    return {"funding": str(funding_hourly), "markPx": "100", "openInterest": "0"}


def _meta_and_ctxs(coin_to_funding: dict[str, float]) -> list:
    universe = [{"name": c} for c in coin_to_funding]
    ctxs = [_ctx(f) for f in coin_to_funding.values()]
    return [{"universe": universe}, ctxs]


def test_refresh_caches_funding_as_apr_pct():
    """Hourly funding 0.0001 → APR pct = 0.0001 * 24 * 365 * 100 = 87.6%."""
    info = MagicMock()
    info.meta_and_asset_ctxs.return_value = _meta_and_ctxs({"BTC": 0.0001, "ETH": -0.00005})
    ft = FundingTracker(info)
    n = ft.refresh()
    assert n == 2
    assert abs(ft.get_apr_pct("BTC") - 87.6) < 0.01
    assert abs(ft.get_apr_pct("ETH") - (-43.8)) < 0.01


def test_get_apr_pct_returns_none_for_outcomes():
    """HIP-4 outcomes have no continuous funding."""
    info = MagicMock()
    info.meta_and_asset_ctxs.return_value = _meta_and_ctxs({"BTC": 0.0})
    ft = FundingTracker(info)
    ft.refresh()
    assert ft.get_apr_pct("#11") is None
    assert ft.get_apr_pct("+11") is None


def test_get_apr_pct_returns_none_for_spot_pairs():
    """Spot pairs (`@230`, `USDH/USDC`) aren't perps."""
    info = MagicMock()
    info.meta_and_asset_ctxs.return_value = _meta_and_ctxs({"BTC": 0.0})
    ft = FundingTracker(info)
    ft.refresh()
    assert ft.get_apr_pct("@230") is None
    assert ft.get_apr_pct("USDH/USDC") is None


def test_get_apr_pct_returns_none_for_unknown_coin():
    info = MagicMock()
    info.meta_and_asset_ctxs.return_value = _meta_and_ctxs({"BTC": 0.0001})
    ft = FundingTracker(info)
    ft.refresh()
    assert ft.get_apr_pct("ETH") is None  # not in cache


def test_refresh_handles_api_failure_keeps_old_cache():
    """If a refresh fails after a successful one, retain the prior snapshot."""
    info = MagicMock()
    info.meta_and_asset_ctxs.return_value = _meta_and_ctxs({"BTC": 0.0001})
    ft = FundingTracker(info)
    ft.refresh()
    btc_before = ft.get_apr_pct("BTC")

    info.meta_and_asset_ctxs.side_effect = RuntimeError("network down")
    ft.refresh()  # should not raise
    assert ft.get_apr_pct("BTC") == btc_before  # unchanged


def test_refresh_handles_unexpected_response_shape():
    info = MagicMock()
    info.meta_and_asset_ctxs.return_value = {"oops": "wrong shape"}
    ft = FundingTracker(info)
    ft.refresh()
    assert ft.get_apr_pct("BTC") is None  # cache stayed empty


def test_refresh_skips_malformed_entries():
    """A bad funding string in one entry shouldn't break the whole refresh."""
    info = MagicMock()
    info.meta_and_asset_ctxs.return_value = [
        {"universe": [{"name": "BTC"}, {"name": "ETH"}, {"name": "BAD"}]},
        [
            {"funding": "0.0001"},
            {"funding": "not a number"},
            {"funding": "0.0002"},
        ],
    ]
    ft = FundingTracker(info)
    ft.refresh()
    assert ft.get_apr_pct("BTC") is not None
    assert ft.get_apr_pct("ETH") is None  # malformed, skipped
    assert ft.get_apr_pct("BAD") is not None  # third entry survived
