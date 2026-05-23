"""Tests for src/backtest.py — leader-edge simulation."""

from __future__ import annotations

from src.backtest import is_perp, simulate_leader


def _fill(coin: str, side: str, sz: float, px: float, closed_pnl: float = 0.0, fee: float = 0.0) -> dict:
    return {
        "coin": coin,
        "side": side,
        "sz": str(sz),
        "px": str(px),
        "closedPnl": str(closed_pnl),
        "fee": str(fee),
        "time": 1700000000000,
    }


# --- is_perp -------------------------------------------------------------


def test_is_perp_accepts_plain_coin():
    assert is_perp("BTC")
    assert is_perp("ETH")
    assert is_perp("xyz:NVDA")  # HIP-3 perp


def test_is_perp_rejects_outcomes():
    assert not is_perp("#42")
    assert not is_perp("+11")


def test_is_perp_rejects_spot():
    assert not is_perp("@230")
    assert not is_perp("USDC/USDH")


def test_is_perp_rejects_empty():
    assert not is_perp("")


# --- simulate_leader -----------------------------------------------------


def _baseline_kwargs():
    return dict(
        proportional_fraction=0.05,
        account_proxy=600.0,
        max_per_trade_usd=75.0,
        min_per_trade_usd=10.0,
    )


def test_zero_fills_returns_zero_stats():
    r = simulate_leader("0xabc", 1.0, fills=[], **_baseline_kwargs())
    assert r.trades == 0
    assert r.closing_fills == 0
    assert r.total_realized == 0.0
    assert r.net == 0.0
    assert r.sharpe == 0.0
    assert r.coins == {}


def test_filters_to_perp_only():
    fills = [
        _fill("BTC", "B", 0.01, 60000, closed_pnl=5.0),
        _fill("#42", "B", 10, 0.55, closed_pnl=100.0),  # outcome — skip
        _fill("@230", "A", 5, 1.0, closed_pnl=0.5),     # spot — skip
        _fill("xyz:NVDA", "B", 0.1, 200, closed_pnl=3.0),
    ]
    r = simulate_leader("0xabc", 1.0, fills=fills, **_baseline_kwargs())
    # 2 perp fills counted, both have non-zero closedPnl
    assert r.trades == 2
    assert r.closing_fills == 2
    assert "BTC" in r.coins
    assert "xyz:NVDA" in r.coins
    assert "#42" not in r.coins
    assert "@230" not in r.coins


def test_scales_pnl_by_size_ratio():
    """Mirror sizing: ratio = our_target_notional / leader_notional, capped at 1."""
    # Leader trades $1000 notional, we'd target 5% × 600 × 1.0 × weight = $30
    # Ratio = $30 / $1000 = 0.03
    fills = [
        _fill("BTC", "B", 1.0, 1000.0, closed_pnl=100.0, fee=1.0),
    ]
    r = simulate_leader("0xabc", 1.0, fills=fills, **_baseline_kwargs())
    # Our PnL: 100.0 * 0.03 = 3.0; fee: 1.0 * 0.03 = 0.03
    assert abs(r.total_realized - 3.0) < 1e-6
    assert abs(r.total_fees - 0.03) < 1e-6
    assert abs(r.net - 2.97) < 1e-6


def test_caps_ratio_at_one_when_leader_smaller_than_target():
    """When the leader's trade is smaller than our target sizing, we cap the
    ratio at 1.0 (we'd mirror their full size, not amplify)."""
    fills = [
        _fill("BTC", "B", 0.001, 60000, closed_pnl=2.0),  # $60 notional, smaller than our $30 target
    ]
    r = simulate_leader("0xabc", 1.0, fills=fills, **_baseline_kwargs())
    # ratio = min($30/$60, 1.0) = 0.5
    assert abs(r.total_realized - 1.0) < 1e-6


def test_weight_amplifies_sizing():
    fills = [
        _fill("BTC", "B", 1.0, 1000.0, closed_pnl=100.0),
    ]
    # weight=2.0 → target = 5% × 600 × 2.0 = $60 (still under $75 cap)
    # ratio = $60/$1000 = 0.06 → PnL = 100*0.06 = 6.0
    r = simulate_leader("0xabc", 2.0, fills=fills, **_baseline_kwargs())
    assert abs(r.total_realized - 6.0) < 1e-6


def test_max_per_trade_caps_target():
    fills = [
        _fill("BTC", "B", 1.0, 10000.0, closed_pnl=100.0),
    ]
    # weight=5.0 would imply target = 5% × 600 × 5.0 = $150, but max_per_trade=$75 caps it
    # ratio = $75/$10000 = 0.0075 → PnL = 100*0.0075 = 0.75
    r = simulate_leader("0xabc", 5.0, fills=fills, **_baseline_kwargs())
    assert abs(r.total_realized - 0.75) < 1e-6


def test_hit_rate_and_sharpe():
    fills = [
        _fill("BTC", "B", 1.0, 1000, closed_pnl=10.0),  # win
        _fill("BTC", "A", 1.0, 1000, closed_pnl=-3.0),  # loss
        _fill("BTC", "B", 1.0, 1000, closed_pnl=5.0),   # win
        _fill("BTC", "A", 1.0, 1000, closed_pnl=8.0),   # win
    ]
    r = simulate_leader("0xabc", 1.0, fills=fills, **_baseline_kwargs())
    assert r.closing_fills == 4
    assert r.hit_rate == 0.75  # 3/4 wins
    # Mean = (10-3+5+8)/4 = 5.0  (then scaled by ratio = 0.03 → 0.15)
    # Sharpe is mean/std on the SCALED values; ratio doesn't change Sharpe (constant scalar)
    # Actually: scaled values are [0.30, -0.09, 0.15, 0.24], mean=0.15, std≈0.144
    # Sharpe ≈ 1.04
    assert r.sharpe > 0.8
    assert r.sharpe < 1.5


def test_max_drawdown_computes_peak_to_trough():
    """Drawdown tracked on cumulative realized series."""
    fills = [
        _fill("BTC", "B", 1.0, 1000, closed_pnl=10.0),   # +10 (peak)
        _fill("BTC", "A", 1.0, 1000, closed_pnl=-5.0),   # +5
        _fill("BTC", "B", 1.0, 1000, closed_pnl=-7.0),   # -2 (trough, dd from 10 = 12)
        _fill("BTC", "A", 1.0, 1000, closed_pnl=20.0),   # +18
    ]
    r = simulate_leader("0xabc", 1.0, fills=fills, **_baseline_kwargs())
    # Scaled by ratio = 0.03: max_dd = 12 * 0.03 = 0.36
    assert abs(r.max_drawdown - 0.36) < 1e-3


def test_coins_breakdown_sums_correctly():
    fills = [
        _fill("BTC", "B", 1.0, 1000, closed_pnl=10.0),
        _fill("BTC", "A", 1.0, 1000, closed_pnl=5.0),
        _fill("ETH", "B", 1.0, 1000, closed_pnl=-3.0),
    ]
    r = simulate_leader("0xabc", 1.0, fills=fills, **_baseline_kwargs())
    # ratio = 0.03
    # BTC: (10 + 5) * 0.03 = 0.45 (no fees in this test)
    # ETH: -3 * 0.03 = -0.09
    assert abs(r.coins["BTC"] - 0.45) < 1e-6
    assert abs(r.coins["ETH"] - (-0.09)) < 1e-6


def test_skips_non_dict_fills():
    fills = [
        _fill("BTC", "B", 1.0, 1000, closed_pnl=10.0),
        "not_a_dict",  # type: ignore[list-item]
        _fill("ETH", "B", 1.0, 1000, closed_pnl=5.0),
    ]
    r = simulate_leader("0xabc", 1.0, fills=fills, **_baseline_kwargs())
    assert r.trades == 2  # the string entry skipped


def test_skips_malformed_numeric_fields():
    fills = [
        {"coin": "BTC", "side": "B", "sz": "abc", "px": "1000", "closedPnl": "10"},
        _fill("ETH", "B", 1.0, 1000, closed_pnl=5.0),
    ]
    r = simulate_leader("0xabc", 1.0, fills=fills, **_baseline_kwargs())
    assert r.trades == 1
