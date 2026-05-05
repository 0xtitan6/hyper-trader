"""Tests for src/leader_score.py — leader quality metrics + filter."""

from __future__ import annotations

import math
from unittest.mock import MagicMock

from src.leader_score import (
    LeaderMetrics,
    _compute_metrics,
    load_metrics,
    meets_quality,
)


def _f(t_ms: int, side: str = "B", closed_pnl: float = 0.0) -> dict:
    return {
        "time": t_ms,
        "side": side,
        "closedPnl": str(closed_pnl),
    }


# ---------- _compute_metrics ----------


def test_empty_fills_returns_zero_metrics():
    m = _compute_metrics(address="0xabc", lookback_hours=168.0, fills=[])
    assert m.trade_count == 0
    assert m.realized_pnl_sharpe == 0.0
    assert m.time_between_fills_p50_s == 0.0
    assert m.direction_consistency == 0.0


def test_single_trade_no_sharpe():
    """Need ≥ 2 closing fills for a meaningful Sharpe."""
    m = _compute_metrics(
        address="0xabc",
        lookback_hours=168,
        fills=[_f(1000, "B", 5.0)],
    )
    assert m.closing_fill_count == 1
    assert m.realized_pnl_sharpe == 0.0  # not enough data


def test_holding_time_p50_seconds():
    """Median time gap between fills, in seconds."""
    fills = [
        _f(1_000_000, "B", 0),
        _f(2_000_000, "A", 1.0),  # 1000s gap
        _f(2_300_000, "B", 0),  # 300s gap
        _f(5_300_000, "A", 1.0),  # 3000s gap
    ]
    m = _compute_metrics(address="0xabc", lookback_hours=168, fills=fills)
    # Three deltas: 1000s, 300s, 3000s → median is 1000
    assert m.time_between_fills_p50_s == 1000.0


def test_scalper_low_holding_time():
    """Sub-second-spaced fills → low p50 → would be filtered."""
    base = 1_000_000
    fills = [_f(base + i * 100, "B", 0.01) for i in range(20)]  # 0.1s apart
    m = _compute_metrics(address="0xscalp", lookback_hours=168, fills=fills)
    assert m.time_between_fills_p50_s == 0.1


def test_swing_trader_high_holding_time():
    """Hour-spaced fills → high p50 → preferred."""
    base = 1_000_000
    fills = [_f(base + i * 3_600_000, "B", 5.0) for i in range(10)]  # 1h apart
    m = _compute_metrics(address="0xswing", lookback_hours=168, fills=fills)
    assert m.time_between_fills_p50_s == 3600.0


def test_realized_pnl_sharpe():
    """Consistent positive PnL → high Sharpe."""
    # 5 wins of similar size — high Sharpe
    fills = [_f(i * 1_000_000, "A", 10.0) for i in range(1, 6)]
    m = _compute_metrics(address="0xabc", lookback_hours=168, fills=fills)
    # mean = 10, std = 0 → sharpe = 0 (per our handling)
    assert m.realized_pnl_sharpe == 0.0  # std=0 protected


def test_realized_pnl_sharpe_with_variance():
    """Mean / std for a simple case."""
    fills = [
        _f(1_000_000, "A", 10.0),
        _f(2_000_000, "A", 12.0),
        _f(3_000_000, "A", 14.0),
        _f(4_000_000, "A", 8.0),
        _f(5_000_000, "A", 6.0),
    ]
    m = _compute_metrics(address="0xabc", lookback_hours=168, fills=fills)
    # mean = 10, pstdev = sqrt(8) ≈ 2.83 → sharpe ≈ 3.54
    assert abs(m.realized_pnl_sharpe - 10 / math.sqrt(8)) < 1e-3


def test_realized_pnl_negative_sharpe_for_losing_leader():
    fills = [
        _f(1_000_000, "A", -10.0),
        _f(2_000_000, "A", -8.0),
        _f(3_000_000, "A", -12.0),
    ]
    m = _compute_metrics(address="0xabc", lookback_hours=168, fills=fills)
    assert m.realized_pnl_sharpe < 0  # losing edge


def test_direction_consistency_pure_long():
    fills = [_f(i * 1000, "B", 1.0) for i in range(1, 11)]  # 10 buys
    m = _compute_metrics(address="0xabc", lookback_hours=168, fills=fills)
    assert m.direction_consistency == 1.0  # 10/10


def test_direction_consistency_flip_flopper():
    """Today's failing leader pattern: alternating buys and sells."""
    fills = []
    for i in range(20):
        fills.append(_f(i * 1000, "B" if i % 2 == 0 else "A", 0.0))
    m = _compute_metrics(address="0xflip", lookback_hours=168, fills=fills)
    assert m.direction_consistency == 0.5  # 10 buys / 10 sells, total 20


def test_drawdown_no_drawdown():
    """Monotonically increasing PnL → zero drawdown."""
    fills = [_f(i * 1000, "A", 5.0) for i in range(1, 5)]  # +5, +5, +5, +5
    m = _compute_metrics(address="0xabc", lookback_hours=168, fills=fills)
    assert m.max_drawdown_usd == 0.0


def test_drawdown_simple():
    """+10, +10, -15 → cum: 10, 20, 5 → drawdown from peak 20 = 15."""
    fills = [
        _f(1_000_000, "A", 10.0),
        _f(2_000_000, "A", 10.0),
        _f(3_000_000, "A", -15.0),
    ]
    m = _compute_metrics(address="0xabc", lookback_hours=168, fills=fills)
    assert m.max_drawdown_usd == 15.0


def test_total_realized_pnl():
    fills = [_f(i * 1000, "A", 1.5) for i in range(1, 11)]
    m = _compute_metrics(address="0xabc", lookback_hours=168, fills=fills)
    assert abs(m.total_realized_pnl_usd - 15.0) < 1e-6


def test_malformed_fills_skipped():
    fills = [
        "not a dict",
        {"time": "garbage", "closedPnl": "5"},  # bad time
        {"time": 1000, "closedPnl": "garbage"},  # bad closedPnl
        {"time": 2000, "side": "B", "closedPnl": "1.0"},  # good
    ]
    m = _compute_metrics(address="0xabc", lookback_hours=168, fills=fills)
    assert m.trade_count == 1  # only the good one
    assert m.total_realized_pnl_usd == 1.0


# ---------- meets_quality ----------


def test_quality_pass_for_good_leader():
    m = LeaderMetrics(
        address="0xabc",
        lookback_hours=168,
        trade_count=50,
        closing_fill_count=25,
        total_realized_pnl_usd=200.0,
        realized_pnl_sharpe=0.8,
        time_between_fills_p50_s=600.0,  # 10 min
        direction_consistency=0.7,
        max_drawdown_usd=20.0,
    )
    ok, reason = meets_quality(m)
    assert ok is True
    assert reason == ""


def test_quality_reject_scalper():
    """Sub-second holding time → reject."""
    m = LeaderMetrics(
        address="0xscalp",
        lookback_hours=168,
        trade_count=200,
        closing_fill_count=100,
        total_realized_pnl_usd=500.0,
        realized_pnl_sharpe=2.0,
        time_between_fills_p50_s=0.5,  # half-second
        direction_consistency=0.6,
        max_drawdown_usd=10.0,
    )
    ok, reason = meets_quality(m)
    assert ok is False
    assert "holding_p50" in reason


def test_quality_reject_flip_flopper():
    """50/50 buy/sell ratio → reject."""
    m = LeaderMetrics(
        address="0xflip",
        lookback_hours=168,
        trade_count=50,
        closing_fill_count=25,
        total_realized_pnl_usd=200.0,
        realized_pnl_sharpe=0.5,
        time_between_fills_p50_s=600.0,
        direction_consistency=0.5,  # exact flip
        max_drawdown_usd=20.0,
    )
    ok, reason = meets_quality(m)
    assert ok is False
    assert "direction" in reason


def test_quality_reject_negative_sharpe():
    m = LeaderMetrics(
        address="0xloser",
        lookback_hours=168,
        trade_count=50,
        closing_fill_count=25,
        total_realized_pnl_usd=-100.0,
        realized_pnl_sharpe=-0.5,
        time_between_fills_p50_s=600.0,
        direction_consistency=0.7,
        max_drawdown_usd=20.0,
    )
    ok, reason = meets_quality(m)
    assert ok is False
    assert "sharpe" in reason


def test_quality_reject_below_min_trades():
    m = LeaderMetrics(
        address="0xnew",
        lookback_hours=168,
        trade_count=5,
        closing_fill_count=2,
        total_realized_pnl_usd=50.0,
        realized_pnl_sharpe=2.0,
        time_between_fills_p50_s=600.0,
        direction_consistency=0.8,
        max_drawdown_usd=10.0,
    )
    ok, reason = meets_quality(m)
    assert ok is False
    assert "trades" in reason


def test_quality_reject_excess_drawdown():
    m = LeaderMetrics(
        address="0xrisky",
        lookback_hours=168,
        trade_count=50,
        closing_fill_count=25,
        total_realized_pnl_usd=200.0,
        realized_pnl_sharpe=0.5,
        time_between_fills_p50_s=600.0,
        direction_consistency=0.7,
        max_drawdown_usd=500.0,
    )
    ok, reason = meets_quality(m, max_drawdown_usd=200.0)
    assert ok is False
    assert "max_drawdown" in reason


def test_quality_thresholds_overridable():
    """Operator can dial thresholds — softer filter when desired."""
    m = LeaderMetrics(
        address="0xfast",
        lookback_hours=168,
        trade_count=200,
        closing_fill_count=100,
        total_realized_pnl_usd=500.0,
        realized_pnl_sharpe=2.0,
        time_between_fills_p50_s=10.0,  # 10s — would fail default 300s
        direction_consistency=0.6,
        max_drawdown_usd=10.0,
    )
    ok_default, _ = meets_quality(m)
    ok_relaxed, _ = meets_quality(m, min_holding_s=5.0)
    assert ok_default is False
    assert ok_relaxed is True


# ---------- load_metrics (I/O surface) ----------


def test_load_metrics_handles_fetch_failure():
    info = MagicMock()
    info.post.side_effect = RuntimeError("network down")
    m = load_metrics(info, "0xabc")
    assert m is None


def test_load_metrics_handles_non_list_response():
    info = MagicMock()
    info.post.return_value = {"oops": "wrong shape"}
    m = load_metrics(info, "0xabc")
    assert m is None


def test_load_metrics_happy_path():
    info = MagicMock()
    info.post.return_value = [
        _f(1_000_000, "B", 0),
        _f(1_500_000, "A", 5.0),
        _f(2_000_000, "B", 0),
        _f(2_500_000, "A", 5.0),
    ]
    m = load_metrics(info, "0xABC", lookback_hours=24)
    assert m is not None
    assert m.address == "0xabc"  # lowercased
    assert m.trade_count == 4
    assert m.closing_fill_count == 2
    assert m.total_realized_pnl_usd == 10.0
    # post called once with userFillsByTime
    info.post.assert_called_once()
    kwargs_payload = info.post.call_args.args[1]
    assert kwargs_payload["type"] == "userFillsByTime"
    assert kwargs_payload["user"] == "0xabc"
