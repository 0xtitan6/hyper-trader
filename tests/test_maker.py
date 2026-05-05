"""Tests for src/maker.py — the HIP-4 outcome market-maker."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from src.errors import OrderError
from src.maker import MakerConfig, OutcomeMaker
from src.market_meta import MarketMeta


@pytest.fixture
def mk_cfg() -> MakerConfig:
    return MakerConfig(
        coin="#20",
        expiry_ts=2_000_000_000,  # far future
        quote_size_shares=1.0,
        min_spread_bps=30.0,
        max_position_shares=20.0,
        max_inventory_usd=5.0,
        cancel_threshold_bps=5.0,
        refresh_interval_s=0.0,  # tests don't sleep
    )


@pytest.fixture
def market_meta_mk() -> MarketMeta:
    info = MagicMock()
    info.meta.return_value = {"universe": []}
    info.spot_meta.return_value = {"universe": [], "tokens": []}
    mm = MarketMeta(info)
    mm.load()
    return mm


def _book(bid: float, ask: float, bid_sz: float = 100, ask_sz: float = 100):
    return {
        "levels": [
            [{"px": str(bid), "sz": str(bid_sz)}],
            [{"px": str(ask), "sz": str(ask_sz)}],
        ]
    }


def _maker(cfg, mm, journal, dry_run=True):
    info = MagicMock()
    exchange = MagicMock()
    exchange.order.return_value = {
        "status": "ok",
        "response": {"type": "order", "data": {"statuses": [{"resting": {"oid": 12345}}]}},
    }
    return OutcomeMaker(
        info=info,
        exchange=exchange,
        market_meta=mm,
        journal=journal,
        config=cfg,
        dry_run=dry_run,
    )


# ---------- spread floor ----------


def test_skip_when_spread_below_floor(mk_cfg, market_meta_mk, journal):
    """Tight market = no quotes. This is the conservative default that
    prevents negative-EV MM after fees."""
    m = _maker(mk_cfg, market_meta_mk, journal)
    # spread = 0.5985 - 0.5984 = 0.0001 → ~1.7 bps, below 30bps floor
    m.info.post.return_value = _book(0.5984, 0.5985)
    m.tick()
    # No quotes placed
    assert m._open.bid_oid is None
    assert m._open.ask_oid is None


def test_quotes_when_spread_meets_floor(mk_cfg, market_meta_mk, journal):
    """Wide market = quote both sides."""
    m = _maker(mk_cfg, market_meta_mk, journal)
    # spread = 0.55 - 0.50 = 0.05 → ~952 bps, well above floor
    m.info.post.return_value = _book(0.50, 0.55)
    m.tick()
    assert m._open.bid_oid is not None
    assert m._open.ask_oid is None  # we have no inventory, so we skip ask


def test_skip_on_empty_book(mk_cfg, market_meta_mk, journal):
    m = _maker(mk_cfg, market_meta_mk, journal)
    m.info.post.return_value = {"levels": [[], []]}
    m.tick()
    assert m._open.bid_oid is None


def test_skip_on_malformed_book(mk_cfg, market_meta_mk, journal):
    m = _maker(mk_cfg, market_meta_mk, journal)
    m.info.post.return_value = {"levels": [[{"px": "not-a-number"}], [{"px": "0.55"}]]}
    m.tick()
    assert m._open.bid_oid is None


# ---------- inventory caps ----------


def test_bid_suppressed_when_at_position_cap(mk_cfg, market_meta_mk, journal):
    """When holding max position, don't grow further. Ask stays active."""
    m = _maker(mk_cfg, market_meta_mk, journal)
    m._inventory_shares = mk_cfg.max_position_shares  # at cap
    m._inventory_cost = 10.0
    m.info.post.return_value = _book(0.50, 0.55)
    m.tick()
    # Only ask should be set; bid suppressed
    assert m._open.bid_oid is None  # no bid placed
    assert m._open.ask_oid is not None


def test_bid_suppressed_when_inventory_cost_at_cap(mk_cfg, market_meta_mk, journal):
    m = _maker(mk_cfg, market_meta_mk, journal)
    m._inventory_shares = 5.0
    m._inventory_cost = mk_cfg.max_inventory_usd  # at $ cap
    m.info.post.return_value = _book(0.50, 0.55)
    m.tick()
    assert m._open.bid_oid is None
    assert m._open.ask_oid is not None  # has shares, can still sell


def test_ask_suppressed_when_no_inventory(mk_cfg, market_meta_mk, journal):
    """Don't sell shares we don't own — never go short."""
    m = _maker(mk_cfg, market_meta_mk, journal)
    m._inventory_shares = 0.0
    m.info.post.return_value = _book(0.50, 0.55)
    m.tick()
    assert m._open.bid_oid is not None  # can buy
    assert m._open.ask_oid is None  # nothing to sell


# ---------- order placement ----------


def test_dry_run_does_not_call_exchange(mk_cfg, market_meta_mk, journal):
    m = _maker(mk_cfg, market_meta_mk, journal, dry_run=True)
    m.info.post.return_value = _book(0.50, 0.55)
    m.tick()
    m.exchange.order.assert_not_called()


def test_live_calls_exchange_with_post_only(mk_cfg, market_meta_mk, journal):
    m = _maker(mk_cfg, market_meta_mk, journal, dry_run=False)
    m._inventory_shares = 5.0  # so both sides quote
    m._inventory_cost = 2.5
    m.info.post.return_value = _book(0.50, 0.55)
    m.tick()
    assert m.exchange.order.call_count == 2  # bid + ask
    for call in m.exchange.order.call_args_list:
        kwargs = call.kwargs
        assert kwargs["order_type"] == {"limit": {"tif": "Alo"}}  # post-only
        assert kwargs["reduce_only"] is False


def test_quotes_dont_cross(mk_cfg, market_meta_mk, journal):
    """If math somehow produces bid >= ask, refuse to quote."""
    cfg = MakerConfig(
        coin="#20",
        expiry_ts=2_000_000_000,
        min_spread_bps=30.0,
        quote_offset_ticks=10000,  # absurdly large offset would cross
        refresh_interval_s=0.0,
    )
    m = _maker(cfg, market_meta_mk, journal)
    m.info.post.return_value = _book(0.50, 0.55)
    m.tick()
    assert m._open.bid_oid is None
    assert m._open.ask_oid is None


def test_quotes_respect_sanity_bounds(mk_cfg, market_meta_mk, journal):
    """Don't quote at extreme prices (likely a market-data glitch)."""
    m = _maker(mk_cfg, market_meta_mk, journal)
    # ask above sanity_max (0.99)
    m.info.post.return_value = _book(0.005, 0.995)
    m.tick()
    assert m._open.bid_oid is None
    assert m._open.ask_oid is None


# ---------- own fill handling ----------


def test_buy_fill_increases_inventory(mk_cfg, market_meta_mk, journal):
    m = _maker(mk_cfg, market_meta_mk, journal)
    m.on_own_fill({"sz": "3.0", "px": "0.50", "side": "B"})
    assert m._inventory_shares == 3.0
    assert m._inventory_cost == 1.50


def test_sell_fill_reduces_inventory(mk_cfg, market_meta_mk, journal):
    m = _maker(mk_cfg, market_meta_mk, journal)
    m._inventory_shares = 5.0
    m._inventory_cost = 2.50
    m.on_own_fill({"sz": "2.0", "px": "0.60", "side": "A"})
    assert m._inventory_shares == 3.0
    # cost basis pro-rata: 5 shares @ avg $0.50, sold 2 → 3 left at avg $0.50 = $1.50
    assert m._inventory_cost == pytest.approx(1.50, abs=1e-6)


def test_malformed_fill_ignored(mk_cfg, market_meta_mk, journal):
    m = _maker(mk_cfg, market_meta_mk, journal)
    m._inventory_shares = 5.0
    m._inventory_cost = 2.50
    m.on_own_fill({"sz": "0", "px": "0.50", "side": "B"})  # zero sz
    m.on_own_fill({"sz": "1.0", "px": "0", "side": "B"})  # zero px
    m.on_own_fill({"sz": "1.0", "px": "0.50", "side": "?"})  # bad side
    m.on_own_fill({"sz": "abc", "px": "0.50", "side": "B"})  # non-numeric
    # State unchanged
    assert m._inventory_shares == 5.0
    assert m._inventory_cost == 2.50


# ---------- cancel + replace ----------


def test_cancel_skipped_when_mid_unchanged(mk_cfg, market_meta_mk, journal):
    """If mid hasn't moved enough, don't churn the book."""
    m = _maker(mk_cfg, market_meta_mk, journal, dry_run=False)
    m._inventory_shares = 5.0
    m._inventory_cost = 2.5
    m.info.post.return_value = _book(0.50, 0.55)
    m.tick()  # initial: bid + ask
    initial_calls = m.exchange.order.call_count
    # Same book → no churn
    m.tick()
    assert m.exchange.order.call_count == initial_calls


def test_cancel_when_mid_moves_above_threshold(mk_cfg, market_meta_mk, journal):
    cfg = MakerConfig(
        coin="#20",
        expiry_ts=2_000_000_000,
        min_spread_bps=30.0,
        cancel_threshold_bps=5.0,
        refresh_interval_s=0.0,
    )
    m = _maker(cfg, market_meta_mk, journal, dry_run=False)
    m._inventory_shares = 5.0
    m._inventory_cost = 2.5
    m.info.post.return_value = _book(0.50, 0.55)
    m.tick()  # first quotes
    initial_orders = m.exchange.order.call_count
    # Mid jumps from 0.525 to 0.575 (~950 bps move)
    m.info.post.return_value = _book(0.55, 0.60)
    m.tick()
    # Cancel + replace → exchange.cancel called, exchange.order called again
    assert m.exchange.cancel.call_count >= 1
    assert m.exchange.order.call_count > initial_orders


# ---------- safety ----------


def test_kill_switch_stops_quoting(mk_cfg, market_meta_mk, journal, tmp_path):
    kill = tmp_path / "KILL"
    kill.touch()
    cfg = MakerConfig(
        coin="#20",
        expiry_ts=2_000_000_000,
        kill_switch_file=str(kill),
        refresh_interval_s=0.0,
    )
    m = _maker(cfg, market_meta_mk, journal)
    assert m._should_stop() is True


def test_expiry_buffer_stops_quoting(mk_cfg, market_meta_mk, journal):
    """Don't quote in last 5 min — settlement risk."""
    import time

    cfg = MakerConfig(
        coin="#20",
        expiry_ts=int(time.time()) + 60,  # 1 min away
        expiry_buffer_s=300,  # 5 min buffer
        refresh_interval_s=0.0,
    )
    m = _maker(cfg, market_meta_mk, journal)
    assert m._should_stop() is True


def test_order_failure_propagates_orderError(mk_cfg, market_meta_mk, journal):
    m = _maker(mk_cfg, market_meta_mk, journal, dry_run=False)
    m._inventory_shares = 5.0
    m._inventory_cost = 2.5
    m.exchange.order.side_effect = RuntimeError("HL rejected")
    m.info.post.return_value = _book(0.50, 0.55)
    with pytest.raises(OrderError):
        m.tick()


def test_oid_extraction_from_order_response(mk_cfg, market_meta_mk, journal):
    """Verify _extract_oid handles HL's actual response shape."""
    resting = {
        "status": "ok",
        "response": {
            "type": "order",
            "data": {"statuses": [{"resting": {"oid": 999, "totalSz": "1"}}]},
        },
    }
    filled = {
        "status": "ok",
        "response": {
            "type": "order",
            "data": {"statuses": [{"filled": {"oid": 888, "totalSz": "1"}}]},
        },
    }
    error = {
        "status": "ok",
        "response": {"type": "order", "data": {"statuses": [{"error": "rejected"}]}},
    }

    assert OutcomeMaker._extract_oid(resting) == 999
    assert OutcomeMaker._extract_oid(filled) == 888
    assert OutcomeMaker._extract_oid(error) is None
    assert OutcomeMaker._extract_oid(None) is None
    assert OutcomeMaker._extract_oid({}) is None
