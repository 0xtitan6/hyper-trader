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


def _maker(cfg, mm, journal, dry_run=True, account_address=""):
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
        account_address=account_address,
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


def test_settlement_losing_side_flattens_inventory(mk_cfg, market_meta_mk, journal):
    # HIP-4 binary outcome settles to 0 for the losing side: px=0, dir=Settlement.
    # Pre-fix this px=0 fill was dropped, leaving phantom inventory forever.
    m = _maker(mk_cfg, market_meta_mk, journal)
    m._inventory_shares = 5.0
    m._inventory_cost = 2.50
    m.on_own_fill({"sz": "5.0", "px": "0", "side": "B", "dir": "Settlement"})
    assert m._inventory_shares == 0.0
    assert m._inventory_cost == 0.0


def test_settlement_winning_side_flattens_inventory(mk_cfg, market_meta_mk, journal):
    # Winning side settles to px=1; the position is still resolved → flatten.
    m = _maker(mk_cfg, market_meta_mk, journal)
    m._inventory_shares = 5.0
    m._inventory_cost = 2.50
    m.on_own_fill({"sz": "5.0", "px": "1", "side": "B", "dir": "Settlement"})
    assert m._inventory_shares == 0.0
    assert m._inventory_cost == 0.0


def test_oversell_clamps_inventory_to_zero(mk_cfg, market_meta_mk, journal):
    # Maker is long-only: selling more than held must clamp at 0, never go
    # negative (which would corrupt the average-cost basis on the next buy).
    m = _maker(mk_cfg, market_meta_mk, journal)
    m._inventory_shares = 3.0
    m._inventory_cost = 1.50
    m.on_own_fill({"sz": "5.0", "px": "0.50", "side": "A"})
    assert m._inventory_shares == 0.0
    assert m._inventory_cost == 0.0


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


# ---------- WS fill routing (handle_ws_fills) ----------
#
# Regression coverage for the wiring bug: standalone main() never subscribed
# userFills to on_own_fill, so live inventory silently stayed at 0 and the
# maker would post bids past its position cap, blind. handle_ws_fills is the
# fix; these lock in its contract.


def _ws(fills: list[dict], snapshot: bool = False) -> dict:
    """Build a userFills WS envelope like the SDK delivers."""
    return {"channel": "userFills", "data": {"fills": fills, "isSnapshot": snapshot}}


def test_ws_fill_updates_inventory(mk_cfg, market_meta_mk, journal):
    """A live fill for our coin flows through to inventory + cost basis."""
    m = _maker(mk_cfg, market_meta_mk, journal)
    m.handle_ws_fills(_ws([{"tid": 1, "coin": "#20", "px": "0.50", "sz": "3.0", "side": "B"}]))
    assert m._inventory_shares == 3.0
    assert m._inventory_cost == 1.50


def test_ws_fill_filters_foreign_coin(mk_cfg, market_meta_mk, journal):
    """userFills spans every market; a fill for another coin must be ignored."""
    m = _maker(mk_cfg, market_meta_mk, journal)
    m.handle_ws_fills(_ws([{"tid": 2, "coin": "#99", "px": "0.50", "sz": "3.0", "side": "B"}]))
    assert m._inventory_shares == 0.0
    assert m._inventory_cost == 0.0


def test_ws_snapshot_fills_not_applied_but_marked_seen(mk_cfg, market_meta_mk, journal):
    """Snapshot = historical fills predating this process. They must not touch
    the from-flat inventory, AND their tids must be remembered so a later live
    replay of the same tid is not double-applied."""
    m = _maker(mk_cfg, market_meta_mk, journal)
    fill = {"tid": 5, "coin": "#20", "px": "0.50", "sz": "3.0", "side": "B"}
    m.handle_ws_fills(_ws([fill], snapshot=True))
    assert m._inventory_shares == 0.0  # not applied
    # Same tid arrives again as a live fill → still skipped (already seen)
    m.handle_ws_fills(_ws([fill], snapshot=False))
    assert m._inventory_shares == 0.0


def test_ws_fill_deduped_by_tid(mk_cfg, market_meta_mk, journal):
    """A WS reconnect can re-deliver the same fill; tid dedupe prevents
    double-counting inventory."""
    m = _maker(mk_cfg, market_meta_mk, journal)
    fill = {"tid": 7, "coin": "#20", "px": "0.50", "sz": "3.0", "side": "B"}
    m.handle_ws_fills(_ws([fill]))
    m.handle_ws_fills(_ws([fill]))  # duplicate delivery
    assert m._inventory_shares == 3.0  # applied exactly once


def test_ws_handles_malformed_envelopes(mk_cfg, market_meta_mk, journal):
    """Garbage in must not crash or mutate state."""
    m = _maker(mk_cfg, market_meta_mk, journal)
    m.handle_ws_fills(None)
    m.handle_ws_fills({})
    m.handle_ws_fills({"data": {}})
    m.handle_ws_fills(_ws(["not-a-dict", 42]))  # type: ignore[list-item]
    m.handle_ws_fills(_ws([{"coin": "#20", "px": "0.50", "sz": "3.0", "side": "B"}]))  # no tid
    # only the last (well-formed, no-tid) fill applies
    assert m._inventory_shares == 3.0


# ---------- integration: blind-inventory bug is actually fixed end to end ----------


def test_integration_fill_unblocks_ask_quote(mk_cfg, market_meta_mk, journal):
    """Full cycle proving the wiring fix: flat maker quotes bid-only; after a
    WS buy fill, the next tick knows it holds inventory and quotes the ask too.
    Before the fix the ask would never appear because inventory stayed 0."""
    m = _maker(mk_cfg, market_meta_mk, journal, dry_run=False)
    m.info.post.return_value = _book(0.50, 0.55)

    # 1) Flat → bid only, no ask (nothing to sell).
    m.tick()
    assert m._open.bid_oid is not None
    assert m._open.ask_oid is None

    # 2) Our bid fills via the WS feed → inventory tracked.
    m.handle_ws_fills(
        _ws([{"tid": 100, "coin": "#20", "px": "0.50", "sz": "5.0", "side": "B"}])
    )
    assert m._inventory_shares == 5.0

    # 3) Next tick now quotes the ask too (we have inventory to offload).
    m.info.post.return_value = _book(0.55, 0.60)  # move mid so reconcile reposts
    m.tick()
    assert m._open.ask_oid is not None


# ---------- on-chain inventory reconcile (reconcile_inventory_from_chain) ----------
#
# The maker's ONLY other inventory source is the live userFills WS stream, which
# can't see a position that existed before the process attached (prior run,
# manual trade, fill that landed before the subscription). Without seeding from
# chain, inventory starts at 0 and the risk caps are checked against the wrong
# number → bids can breach the exposure cap. These lock in the seed behavior.


def _user_state_perp(coin: str, szi: float, entry_px: float) -> dict:
    """assetPositions shape (perp/futures surface, keyed by `#NN`)."""
    return {
        "assetPositions": [
            {"position": {"coin": coin, "szi": str(szi), "entryPx": str(entry_px)}}
        ]
    }


def _spot_state(coin: str, total: float, entry_ntl: float) -> dict:
    """spotClearinghouseState shape (HIP-4 outcome surface, keyed by `+NN`)."""
    return {"balances": [{"coin": coin, "total": str(total), "entryNtl": str(entry_ntl)}]}


def test_reconcile_sets_inventory_from_perp_position(mk_cfg, market_meta_mk, journal):
    """An existing on-chain position in our coin seeds inventory + cost basis."""
    m = _maker(mk_cfg, market_meta_mk, journal, account_address="0xabc")
    m.info.user_state.return_value = _user_state_perp("#20", 7.0, 0.40)
    m.reconcile_inventory_from_chain()
    assert m._inventory_shares == 7.0
    assert m._inventory_cost == pytest.approx(2.80)  # 7 * 0.40


def test_reconcile_sets_inventory_from_spot_balance(mk_cfg, market_meta_mk, journal):
    """HIP-4 outcomes live in spotClearinghouseState as `+NN`; reconcile reads
    them and maps `+20` -> our trade coin `#20`."""
    m = _maker(mk_cfg, market_meta_mk, journal, account_address="0xabc")
    m.info.user_state.return_value = {"assetPositions": []}  # not on perp surface
    m.info.post.return_value = _spot_state("+20", 4.0, 2.00)
    m.reconcile_inventory_from_chain()
    assert m._inventory_shares == 4.0
    assert m._inventory_cost == pytest.approx(2.00)  # entryNtl is total cost


def test_reconcile_leaves_inventory_zero_when_coin_absent(mk_cfg, market_meta_mk, journal):
    """No position for our coin upstream → inventory stays flat at 0."""
    m = _maker(mk_cfg, market_meta_mk, journal, account_address="0xabc")
    m.info.user_state.return_value = _user_state_perp("#99", 5.0, 0.50)  # different coin
    m.info.post.return_value = _spot_state("+99", 5.0, 2.5)  # different coin
    m.reconcile_inventory_from_chain()
    assert m._inventory_shares == 0.0
    assert m._inventory_cost == 0.0


def test_reconcile_does_not_crash_when_user_state_raises(mk_cfg, market_meta_mk, journal):
    """A fetch failure must not crash startup — inventory left at 0."""
    m = _maker(mk_cfg, market_meta_mk, journal, account_address="0xabc")
    m.info.user_state.side_effect = RuntimeError("HL down")
    m.reconcile_inventory_from_chain()  # must not raise
    assert m._inventory_shares == 0.0
    assert m._inventory_cost == 0.0


def test_reconcile_does_not_crash_on_garbage(mk_cfg, market_meta_mk, journal):
    """Malformed upstream payloads must not crash or corrupt inventory."""
    m = _maker(mk_cfg, market_meta_mk, journal, account_address="0xabc")
    m.info.user_state.return_value = {"assetPositions": "not-a-list"}
    m.info.post.return_value = {"balances": [{"coin": "+20", "total": "abc"}]}
    m.reconcile_inventory_from_chain()  # must not raise
    assert m._inventory_shares == 0.0
    assert m._inventory_cost == 0.0


def test_reconcile_clamps_short_to_zero(mk_cfg, market_meta_mk, journal):
    """Maker is long-only; a negative upstream szi is clamped to 0."""
    m = _maker(mk_cfg, market_meta_mk, journal, account_address="0xabc")
    m.info.user_state.return_value = _user_state_perp("#20", -3.0, 0.50)
    m.reconcile_inventory_from_chain()
    assert m._inventory_shares == 0.0
    assert m._inventory_cost == 0.0


def test_reconcile_skipped_when_no_address(mk_cfg, market_meta_mk, journal):
    """Empty address (e.g. existing tests) → reconcile is a no-op, no fetch."""
    m = _maker(mk_cfg, market_meta_mk, journal)  # account_address defaults to ""
    m.reconcile_inventory_from_chain()
    m.info.user_state.assert_not_called()
    assert m._inventory_shares == 0.0


def test_run_reconciles_before_quoting(mk_cfg, market_meta_mk, journal, tmp_path):
    """run() must seed inventory from chain BEFORE the loop so caps are correct
    from the first tick. We make _should_stop fire immediately and assert the
    reconcile already happened by then."""
    kill = tmp_path / "KILL"
    cfg = MakerConfig(
        coin="#20",
        expiry_ts=2_000_000_000,
        kill_switch_file=str(kill),
        refresh_interval_s=0.0,
    )
    m = _maker(cfg, market_meta_mk, journal, account_address="0xabc")
    m.info.user_state.return_value = _user_state_perp("#20", 6.0, 0.50)

    seen: dict[str, float] = {}

    def _stop() -> bool:
        # Capture inventory at the moment the loop is first evaluated.
        seen["shares"] = m._inventory_shares
        return True

    m._should_stop = _stop  # type: ignore[method-assign]
    m.run()
    assert seen["shares"] == 6.0  # reconcile ran before the loop


# ---------- self-quote avoidance (strip own resting orders from the book) ----------
#
# HIGH-severity bug: _fetch_book polls l2Book, which includes our OWN resting
# quote. If we don't strip it, the best bid/ask we read back can be our own
# order — and we'd undercut ourselves every tick, a monotonic price walk away
# from the real market that gives away edge. _strip_own_level removes our resting
# size (matched by price) before best_bid/best_ask are derived.


def _book_multi(bid_levels, ask_levels):
    """Build a book from explicit [(px, sz), ...] lists for each side."""
    return {
        "levels": [
            [{"px": str(px), "sz": str(sz)} for px, sz in bid_levels],
            [{"px": str(px), "sz": str(sz)} for px, sz in ask_levels],
        ]
    }


def test_own_ask_not_undercut_uses_next_external_level(mk_cfg, market_meta_mk, journal):
    """When our resting ask is the top of book, quote math must price off the
    NEXT (external) ask level, not our own order — otherwise we undercut
    ourselves toward zero."""
    m = _maker(mk_cfg, market_meta_mk, journal)
    m._inventory_shares = 5.0  # so the ask is active
    m._inventory_cost = 2.5
    # Our ask of size 1 rests at 0.55 (top); the real external ask is 0.60.
    m._open.ask_oid = -1
    m._open.ask_px = 0.55
    book = _book_multi([(0.50, 100)], [(0.55, 1), (0.60, 100)])
    best_bid, best_ask = _derive_best(m, book)
    assert best_ask == pytest.approx(0.60)  # external level, not our 0.55
    assert best_bid == pytest.approx(0.50)


def test_own_bid_not_undercut_uses_next_external_level(mk_cfg, market_meta_mk, journal):
    """Symmetric: our resting bid at top must be ignored; price off the next
    external bid level."""
    m = _maker(mk_cfg, market_meta_mk, journal)
    # Our bid of size 1 rests at 0.50 (top); the real external bid is 0.45.
    m._open.bid_oid = -1
    m._open.bid_px = 0.50
    book = _book_multi([(0.50, 1), (0.45, 100)], [(0.55, 100)])
    best_bid, best_ask = _derive_best(m, book)
    assert best_bid == pytest.approx(0.45)  # external level, not our 0.50
    assert best_ask == pytest.approx(0.55)


def test_own_level_with_external_size_subtracts_keeps_level(mk_cfg, market_meta_mk, journal):
    """When others rest at our price too, we subtract OUR size and keep the
    level (still the external best). Expectation: the level stays as best_ask
    because external traders remain there."""
    m = _maker(mk_cfg, market_meta_mk, journal)
    m._inventory_shares = 5.0
    m._inventory_cost = 2.5
    # 10 total at 0.55, of which 1 is ours → 9 external remain → still best_ask.
    m._open.ask_oid = -1
    m._open.ask_px = 0.55
    book = _book_multi([(0.50, 100)], [(0.55, 10), (0.60, 100)])
    best_bid, best_ask = _derive_best(m, book)
    assert best_ask == pytest.approx(0.55)  # external residual keeps the level
    # And the residual size is reflected (10 - 1 = 9).
    stripped = m._strip_own_level(book["levels"][1], m._open.ask_oid, m._open.ask_px)
    assert float(stripped[0]["sz"]) == pytest.approx(9.0)


def test_own_level_sole_resting_empties_side_is_safe_skip(mk_cfg, market_meta_mk, journal):
    """If stripping our order empties a side (we were the only resters and there
    is no other level), it's treated like a one-sided book → no quote / safe
    skip, exactly like the empty-book path."""
    m = _maker(mk_cfg, market_meta_mk, journal)
    m._inventory_shares = 5.0
    m._inventory_cost = 2.5
    # Our ask is the ONLY ask level; stripping it empties the ask side.
    m._open.ask_oid = -1
    m._open.ask_px = 0.55
    m.info.post.return_value = _book_multi([(0.50, 100)], [(0.55, 1)])
    # Confirm the stripped ask side is empty → tick takes the empty-book skip.
    assert m._strip_own_level(
        m.info.post.return_value["levels"][1], m._open.ask_oid, m._open.ask_px
    ) == []
    m.tick()
    # Empty side after strip → skip, no NEW bid placed and exchange untouched.
    assert m._open.bid_oid is None
    m.exchange.order.assert_not_called()


def test_book_with_no_own_orders_unchanged(mk_cfg, market_meta_mk, journal):
    """Regression guard: with no resting orders (oids None), the book passes
    through untouched and behavior matches the pre-fix path."""
    m = _maker(mk_cfg, market_meta_mk, journal)
    assert m._open.bid_oid is None and m._open.ask_oid is None
    book = _book(0.50, 0.55)
    bids = m._strip_own_level(book["levels"][0], m._open.bid_oid, m._open.bid_px)
    asks = m._strip_own_level(book["levels"][1], m._open.ask_oid, m._open.ask_px)
    assert bids == book["levels"][0]
    assert asks == book["levels"][1]


def _derive_best(m, book):
    """Run the tick() book-cleaning path and return (best_bid, best_ask)."""
    bids = m._strip_own_level(book["levels"][0], m._open.bid_oid, m._open.bid_px)
    asks = m._strip_own_level(book["levels"][1], m._open.ask_oid, m._open.ask_px)
    return float(bids[0]["px"]), float(asks[0]["px"])


# ---------- _cancel_all failure handling (orphaned-order safety) ----------


def _journal_events(journal) -> list[dict]:
    import json
    from pathlib import Path

    p = Path(journal.path)
    if not p.exists():
        return []
    return [json.loads(line) for line in p.read_text().splitlines() if line.strip()]


def test_cancel_failure_keeps_oid_and_journals(mk_cfg, market_meta_mk, journal):
    """A transient cancel failure must NOT clear the local oid (orphaned-order
    risk), and must write a maker_cancel_failed journal event."""
    m = _maker(mk_cfg, market_meta_mk, journal, dry_run=False)
    m._open.bid_oid = 111
    m._open.bid_px = 0.50
    m.exchange.cancel.side_effect = RuntimeError("429 rate limited")

    m._cancel_all("reposting")

    # oid stays tracked so a later cycle can retry the cancel
    assert m._open.bid_oid == 111
    assert m._open.bid_px == 0.50

    events = _journal_events(journal)
    failed = [e for e in events if e["event"] == "maker_cancel_failed"]
    assert len(failed) == 1
    assert failed[0]["coin"] == "#20"
    assert failed[0]["oid"] == 111


def test_cancel_failure_is_retried_next_call(mk_cfg, market_meta_mk, journal):
    """Because the oid is kept, a second _cancel_all retries exchange.cancel for
    the same oid (orphan never goes untracked)."""
    m = _maker(mk_cfg, market_meta_mk, journal, dry_run=False)
    m._open.ask_oid = 222
    m._open.ask_px = 0.55
    m.exchange.cancel.side_effect = RuntimeError("timeout")

    m._cancel_all("reposting")
    m._cancel_all("reposting")

    # both calls attempted to cancel the same resting order
    assert m.exchange.cancel.call_count == 2
    assert all(call.args == ("#20", 222) for call in m.exchange.cancel.call_args_list)
    assert m._open.ask_oid == 222  # still tracked


def test_reconcile_skips_side_when_cancel_failed(mk_cfg, market_meta_mk, journal):
    """If a side's cancel fails, _reconcile must NOT place a new order on that
    side (would double exposure with the still-resting orphan)."""
    m = _maker(mk_cfg, market_meta_mk, journal, dry_run=False)
    m._inventory_shares = 5.0
    m._inventory_cost = 2.5
    m.info.post.return_value = _book(0.50, 0.55)
    m.tick()  # first quotes: bid + ask
    initial_orders = m.exchange.order.call_count
    bid_oid_before = m._open.bid_oid
    ask_oid_before = m._open.ask_oid
    assert bid_oid_before is not None and ask_oid_before is not None

    # Next reconcile: mid jumps so it reposts, but every cancel fails.
    m.exchange.cancel.side_effect = RuntimeError("503")
    m.info.post.return_value = _book(0.55, 0.60)
    m.tick()

    # Cancels were attempted but failed → no NEW orders placed (no double-up).
    assert m.exchange.cancel.call_count >= 1
    assert m.exchange.order.call_count == initial_orders
    # Old oids remain tracked for retry.
    assert m._open.bid_oid == bid_oid_before
    assert m._open.ask_oid == ask_oid_before


def test_cancel_success_clears_oid_and_allows_replace(mk_cfg, market_meta_mk, journal):
    """Success path unchanged: a successful cancel clears the oid and a new
    order is placed on repost."""
    m = _maker(mk_cfg, market_meta_mk, journal, dry_run=False)
    m._inventory_shares = 5.0
    m._inventory_cost = 2.5
    m.info.post.return_value = _book(0.50, 0.55)
    m.tick()
    initial_orders = m.exchange.order.call_count

    # cancel succeeds (default MagicMock) → repost places new orders.
    m.info.post.return_value = _book(0.55, 0.60)
    m.tick()

    assert m.exchange.cancel.call_count >= 1
    assert m.exchange.order.call_count > initial_orders
    assert m._open.bid_oid is not None  # newly placed


def test_cancel_dry_run_does_not_call_exchange(mk_cfg, market_meta_mk, journal):
    """Dry-run path unchanged: oid == -1, exchange.cancel never called, oid
    cleared locally."""
    m = _maker(mk_cfg, market_meta_mk, journal, dry_run=True)
    m._open.bid_oid = -1
    m._open.bid_px = 0.50
    m._open.ask_oid = -1
    m._open.ask_px = 0.55

    m._cancel_all("reposting")

    m.exchange.cancel.assert_not_called()
    assert m._open.bid_oid is None
    assert m._open.ask_oid is None


# ---------- GLFT vol-aware inventory skew ----------


def test_sigma_arithmetic_return_matches_stdev(mk_cfg, market_meta_mk, journal):
    """sigma_raw is the sample stdev of ARITHMETIC per-tick returns.

    At mid=0.5 the Bernoulli factor is 1, so sigma_eff equals that stdev."""
    import statistics

    m = _maker(mk_cfg, market_meta_mk, journal)
    mids = [0.50, 0.52, 0.49, 0.53, 0.50, 0.54, 0.48, 0.52, 0.51, 0.50, 0.50]
    for x in mids:
        m._mids.append(x)
    diffs = [mids[i] - mids[i - 1] for i in range(1, len(mids))]
    expected_raw = statistics.stdev(diffs)
    # At mid=0.5 the Bernoulli factor is exactly 1, so sigma_eff == sigma_raw.
    sigma_eff = m._estimate_sigma(0.5)
    assert sigma_eff == pytest.approx(expected_raw)


def test_bernoulli_adjustment_unity_at_half_smaller_at_boundary(
    mk_cfg, market_meta_mk, journal
):
    """sigma_eff == sigma_raw at mid=0.5; strictly smaller near a boundary."""
    import statistics

    m = _maker(mk_cfg, market_meta_mk, journal)
    mids = [0.50, 0.52, 0.49, 0.53, 0.50, 0.54, 0.48, 0.52, 0.51, 0.50, 0.50]
    for x in mids:
        m._mids.append(x)
    diffs = [mids[i] - mids[i - 1] for i in range(1, len(mids))]
    sigma_raw = statistics.stdev(diffs)

    at_half = m._estimate_sigma(0.5)
    near_boundary = m._estimate_sigma(0.1)

    assert at_half == pytest.approx(sigma_raw)
    # sqrt(0.1*0.9)/0.5 = 0.6 < 1 → collapses
    assert near_boundary == pytest.approx(sigma_raw * (0.1 * 0.9) ** 0.5 / 0.5)
    assert near_boundary < at_half


def test_sigma_falls_back_to_base_with_few_samples(mk_cfg, market_meta_mk, journal):
    """Fewer than ~10 return samples → use sigma_base (at mid=0.5 → unchanged)."""
    m = _maker(mk_cfg, market_meta_mk, journal)
    for x in [0.50, 0.51, 0.49, 0.50]:  # only 3 diffs
        m._mids.append(x)
    assert m._estimate_sigma(0.5) == pytest.approx(mk_cfg.sigma_base)


def test_glft_skew_monotonic_in_inventory(mk_cfg, market_meta_mk, journal):
    """GLFT skew grows with inventory q."""
    m = _maker(mk_cfg, market_meta_mk, journal)
    c2 = m._glft_c2()
    sigma = 0.03
    low = (1.0 / mk_cfg.max_position_shares) * sigma * c2
    high = (10.0 / mk_cfg.max_position_shares) * sigma * c2
    assert high > low > 0


def test_glft_skew_monotonic_in_sigma(mk_cfg, market_meta_mk, journal):
    """GLFT skew grows with sigma_eff for fixed inventory."""
    m = _maker(mk_cfg, market_meta_mk, journal)
    m._inventory_shares = 10.0
    m._inventory_cost = 0.0
    bid_lo, ask_lo = m._compute_quotes(0.5, 0.50, 0.55, sigma_eff=0.01)
    bid_hi, ask_hi = m._compute_quotes(0.5, 0.50, 0.55, sigma_eff=0.05)
    # Higher sigma → larger downward skew → lower bid and ask.
    assert bid_hi < bid_lo
    assert ask_hi < ask_lo


def test_glft_skew_walks_both_sides_down_when_long(mk_cfg, market_meta_mk, journal):
    """Holding inventory pushes BOTH bid and ask below the no-skew quotes.

    We hold inventory in both cases (so the ask side is active either way) and
    compare sigma_eff=0 (no skew) against sigma_eff>0 (skew on)."""
    m = _maker(mk_cfg, market_meta_mk, journal)
    m._inventory_shares = 10.0
    m._inventory_cost = 0.0
    bid0, ask0 = m._compute_quotes(0.5, 0.50, 0.55, sigma_eff=0.0)
    bid1, ask1 = m._compute_quotes(0.5, 0.50, 0.55, sigma_eff=0.05)
    assert bid1 < bid0
    assert ask1 < ask0


def test_estimate_sigma_mid_at_boundary_does_not_crash(mk_cfg, market_meta_mk, journal):
    """mid at exactly 0 or 1 is guarded: sigma_eff collapses to 0, no crash."""
    m = _maker(mk_cfg, market_meta_mk, journal)
    for x in [0.50, 0.52, 0.49, 0.53, 0.50, 0.54, 0.48, 0.52, 0.51, 0.50, 0.50]:
        m._mids.append(x)
    assert m._estimate_sigma(0.0) == 0.0
    assert m._estimate_sigma(1.0) == 0.0


def test_tick_with_extreme_mid_does_not_crash(mk_cfg, market_meta_mk, journal):
    """A full tick at a near-boundary mid runs without raising."""
    m = _maker(mk_cfg, market_meta_mk, journal)
    m.info.post.return_value = _book(0.001, 0.05)  # very low, wide
    m.tick()  # should not raise


def test_use_glft_false_reproduces_legacy_linear_skew(
    market_meta_mk, journal
):
    """With use_glft_skew=False, the old linear skew is reproduced exactly."""
    cfg = MakerConfig(
        coin="#20",
        expiry_ts=2_000_000_000,
        quote_size_shares=1.0,
        min_spread_bps=30.0,
        max_position_shares=20.0,
        max_inventory_usd=5.0,
        refresh_interval_s=0.0,
        use_glft_skew=False,
        inventory_skew_bps_at_full=20.0,
    )
    m = _maker(cfg, market_meta_mk, journal)
    m._inventory_shares = 10.0
    m._inventory_cost = 0.0
    bid_px, ask_px = m._compute_quotes(0.5, 0.50, 0.55, sigma_eff=0.99)

    # Recompute the legacy skew by hand and compare rounded prices.
    q = 10.0 / 20.0
    skew = (q * 20.0 / 10_000.0) * 0.5
    exp_bid = m.market_meta.round_price(0.50 + 1e-5 * cfg.quote_offset_ticks - skew)
    exp_ask = m.market_meta.round_price(0.55 - 1e-5 * cfg.quote_offset_ticks - skew)
    assert bid_px == pytest.approx(exp_bid)
    assert ask_px == pytest.approx(exp_ask)


# ---------- match-event gating ----------

# A fixed fixture window centered on a stable timestamp so the tests don't
# depend on wall-clock. kickoff at KO, est_end an hour later.
_KO = 1_700_000_000
_END = _KO + 3600


def _gate_cfg(**overrides):
    """MakerConfig with one inline fixture and the gate enabled by default."""
    base = dict(
        coin="#20",
        expiry_ts=2_000_000_000,
        quote_size_shares=1.0,
        min_spread_bps=30.0,
        max_position_shares=20.0,
        max_inventory_usd=5.0,
        refresh_interval_s=0.0,
        match_gate_enabled=True,
        match_gate_preroll_s=120,
        match_gate_cooldown_s=300,
        match_gate_fixtures=(
            {
                "match_id": "wc-1",
                "teams": ["ARG", "FRA"],
                "kickoff_ts": _KO,
                "est_end_ts": _END,
            },
        ),
    )
    base.update(overrides)
    return MakerConfig(**base)


def test_match_gate_disabled_quotes_inside_window(market_meta_mk, journal, monkeypatch):
    """Gate disabled → even if `now` is inside a window, we quote normally."""
    import time as _time

    cfg = _gate_cfg(match_gate_enabled=False)
    m = _maker(cfg, market_meta_mk, journal)
    monkeypatch.setattr(_time, "time", lambda: float(_KO + 60))  # mid-match
    m.info.post.return_value = _book(0.50, 0.55)
    m.tick()
    assert m._match_gate_active(_KO + 60) is False
    m.info.post.assert_called()  # book was fetched
    assert m._open.bid_oid is not None  # quoted as usual


def test_match_gate_active_skips_tick_and_book(market_meta_mk, journal, monkeypatch):
    """Inside [kickoff-preroll, end+cooldown] → no quotes AND no book fetch."""
    import time as _time

    cfg = _gate_cfg()
    m = _maker(cfg, market_meta_mk, journal)
    monkeypatch.setattr(_time, "time", lambda: float(_KO + 60))
    m.info.post.return_value = _book(0.50, 0.55)
    m.tick()
    m.info.post.assert_not_called()  # _fetch_book never reached
    assert m._open.bid_oid is None
    assert m._open.ask_oid is None


def test_match_gate_entering_window_cancels_resting(market_meta_mk, journal, monkeypatch):
    """First tick (clear) sets oids; entering the window cancels them next tick."""
    import time as _time

    cfg = _gate_cfg()
    m = _maker(cfg, market_meta_mk, journal)
    m.info.post.return_value = _book(0.50, 0.55)

    # First tick well before the window → quotes rest.
    monkeypatch.setattr(_time, "time", lambda: float(_KO - 10_000))
    m.tick()
    assert m._open.bid_oid is not None

    # Advance into the window → gate fires, _cancel_all clears resting oids.
    cancel_spy = MagicMock(wraps=m._cancel_all)
    monkeypatch.setattr(m, "_cancel_all", cancel_spy)
    monkeypatch.setattr(_time, "time", lambda: float(_KO + 60))
    m.tick()
    cancel_spy.assert_called_with("match_gate")
    assert m._open.bid_oid is None
    assert m._open.ask_oid is None


def test_match_gate_before_preroll_and_after_cooldown_quotes(
    market_meta_mk, journal, monkeypatch
):
    """Outside the window (well before preroll, well after cooldown) → quote."""
    import time as _time

    cfg = _gate_cfg()
    m = _maker(cfg, market_meta_mk, journal)
    m.info.post.return_value = _book(0.50, 0.55)

    # Well before preroll.
    monkeypatch.setattr(_time, "time", lambda: float(_KO - 10_000))
    m.tick()
    assert m._open.bid_oid is not None

    # Reset and test well after cooldown.
    m._open.bid_oid = None
    m._open.bid_px = 0.0
    m._open.last_quote_at = 0.0
    m._open.last_mid = 0.0
    monkeypatch.setattr(_time, "time", lambda: float(_END + 10_000))
    m.tick()
    assert m._open.bid_oid is not None


def test_match_gate_preroll_boundary(market_meta_mk, journal):
    """now == kickoff-preroll is gated; one second earlier is clear."""
    cfg = _gate_cfg()
    m = _maker(cfg, market_meta_mk, journal)
    start = _KO - cfg.match_gate_preroll_s
    assert m._match_gate_active(start) is True
    assert m._match_gate_active(start - 1) is False


def test_match_gate_cooldown_boundary(market_meta_mk, journal):
    """now == end+cooldown is gated; one second later is clear."""
    cfg = _gate_cfg()
    m = _maker(cfg, market_meta_mk, journal)
    end = _END + cfg.match_gate_cooldown_s
    assert m._match_gate_active(end) is True
    assert m._match_gate_active(end + 1) is False


def test_match_gate_fixtures_from_file(market_meta_mk, journal, tmp_path):
    """Fixtures loaded from a JSON file are honored by the gate."""
    import json
    from pathlib import Path

    fixtures_path = Path(tmp_path) / "fixtures.json"
    fixtures_path.write_text(
        json.dumps(
            [
                {
                    "match_id": "wc-2",
                    "teams": ["BRA", "GER"],
                    "kickoff_ts": _KO,
                    "est_end_ts": _END,
                }
            ]
        )
    )
    # File path wins over inline list (give a bogus inline fixture far away).
    cfg = _gate_cfg(
        match_gate_fixtures_file=str(fixtures_path),
        match_gate_fixtures=(),
    )
    m = _maker(cfg, market_meta_mk, journal)
    assert m._match_gate_active(_KO + 60) is True
    assert m._match_gate_active(_KO - 10_000) is False


def test_match_gate_malformed_fixture_skipped(market_meta_mk, journal):
    """One malformed + one valid fixture → valid still gates, no exception."""
    cfg = _gate_cfg(
        match_gate_fixtures=(
            {"match_id": "bad", "teams": ["X", "Y"], "kickoff_ts": "oops"},  # non-int
            {
                "match_id": "good",
                "teams": ["ARG", "FRA"],
                "kickoff_ts": _KO,
                "est_end_ts": _END,
            },
        )
    )
    m = _maker(cfg, market_meta_mk, journal)  # must not raise
    # Only the valid fixture produced a window.
    assert len(m._match_windows) == 1
    assert m._match_gate_active(_KO + 60) is True
    events = _journal_events(journal)
    assert any(e["event"] == "match_gate_bad_fixture" for e in events)


def test_match_gate_missing_file_empty_inline_failclosed(
    market_meta_mk, journal, monkeypatch, tmp_path
):
    """Enabled + fixtures_file points at a nonexistent path + empty inline →
    fail CLOSED: degraded flag set, gate active for ALL times, the
    `match_gate_degraded_failclosed` event is journalled, and tick() quotes
    nothing (no book fetch, no resting orders)."""
    import time as _time
    from pathlib import Path

    missing = Path(tmp_path) / "does_not_exist.json"
    cfg = _gate_cfg(
        match_gate_fixtures_file=str(missing),
        match_gate_fixtures=(),
    )
    m = _maker(cfg, market_meta_mk, journal)

    assert m._match_gate_degraded is True
    # Gate is active for any time — even far outside any real window.
    assert m._match_gate_active(_KO + 60) is True
    assert m._match_gate_active(_KO - 10_000_000) is True
    assert m._match_gate_active(_END + 10_000_000) is True

    events = _journal_events(journal)
    assert any(e["event"] == "match_gate_degraded_failclosed" for e in events)
    # The underlying file failure is still surfaced.
    assert any(e["event"] == "match_gate_file_unreadable" for e in events)

    # tick() must place no quotes and never fetch the book.
    monkeypatch.setattr(_time, "time", lambda: float(_KO - 10_000_000))
    m.info.post.return_value = _book(0.50, 0.55)
    m.tick()
    m.info.post.assert_not_called()
    assert m._open.bid_oid is None
    assert m._open.ask_oid is None


def test_match_gate_unreadable_file_nonempty_inline_uses_inline(
    market_meta_mk, journal, tmp_path
):
    """Enabled + unreadable file + NON-empty inline fixtures → real fallback:
    NOT degraded, gates by the inline windows correctly."""
    from pathlib import Path

    missing = Path(tmp_path) / "nope.json"
    # _gate_cfg's default inline fixture is the ARG/FRA window at [_KO, _END].
    cfg = _gate_cfg(match_gate_fixtures_file=str(missing))
    m = _maker(cfg, market_meta_mk, journal)

    assert m._match_gate_degraded is False
    # Gated inside the inline window, clear well outside it.
    assert m._match_gate_active(_KO + 60) is True
    assert m._match_gate_active(_KO - 10_000) is False
    assert m._match_gate_active(_END + 10_000) is False
    # File failure is still journalled, but no degraded event.
    events = _journal_events(journal)
    assert any(e["event"] == "match_gate_file_unreadable" for e in events)
    assert not any(e["event"] == "match_gate_degraded_failclosed" for e in events)


def test_match_gate_valid_file_not_degraded(market_meta_mk, journal, tmp_path):
    """Enabled + valid fixtures file → normal windows, not degraded."""
    import json
    from pathlib import Path

    fixtures_path = Path(tmp_path) / "fixtures.json"
    fixtures_path.write_text(
        json.dumps(
            [
                {
                    "match_id": "wc-3",
                    "teams": ["ESP", "POR"],
                    "kickoff_ts": _KO,
                    "est_end_ts": _END,
                }
            ]
        )
    )
    cfg = _gate_cfg(
        match_gate_fixtures_file=str(fixtures_path),
        match_gate_fixtures=(),
    )
    m = _maker(cfg, market_meta_mk, journal)

    assert m._match_gate_degraded is False
    assert m._match_gate_active(_KO + 60) is True
    assert m._match_gate_active(_KO - 10_000) is False
    events = _journal_events(journal)
    assert not any(e["event"] == "match_gate_degraded_failclosed" for e in events)


def test_match_gate_disabled_never_degraded(market_meta_mk, journal, monkeypatch):
    """Disabled gate → never degraded, quotes normally even with a bad file."""
    import time as _time
    from pathlib import Path

    missing = Path("/this/path/does/not/exist.json")
    cfg = _gate_cfg(
        match_gate_enabled=False,
        match_gate_fixtures_file=str(missing),
        match_gate_fixtures=(),
    )
    m = _maker(cfg, market_meta_mk, journal)

    assert m._match_gate_degraded is False
    assert m._match_gate_active(_KO + 60) is False
    # And it quotes as usual.
    monkeypatch.setattr(_time, "time", lambda: float(_KO + 60))
    m.info.post.return_value = _book(0.50, 0.55)
    m.tick()
    m.info.post.assert_called()
    assert m._open.bid_oid is not None


def test_match_gate_enabled_no_fixtures_failclosed(market_meta_mk, journal):
    """Enabled + no file + empty inline → misconfig, fail CLOSED (degraded)."""
    cfg = _gate_cfg(match_gate_fixtures_file="", match_gate_fixtures=())
    m = _maker(cfg, market_meta_mk, journal)

    assert m._match_gate_degraded is True
    assert m._match_gate_active(_KO + 60) is True
    assert m._match_gate_active(_KO - 10_000_000) is True
    events = _journal_events(journal)
    assert any(e["event"] == "match_gate_degraded_failclosed" for e in events)


# ---------- favorite-longshot NO-skew edge ----------


def _noskew_cfg(coin="#20", **over):
    """MakerConfig with the NO-skew edge enabled (defaults to spec anchors)."""
    base = dict(
        coin=coin,
        expiry_ts=2_000_000_000,
        quote_size_shares=1.0,
        min_spread_bps=30.0,
        max_position_shares=20.0,
        max_inventory_usd=5.0,
        refresh_interval_s=0.0,
        noskew_enabled=True,
    )
    base.update(over)
    return MakerConfig(**base)


def test_noskew_disabled_byte_identical(market_meta_mk, journal):
    """Edge off → quotes byte-identical to baseline (clamp can't change zero)."""
    base_cfg = MakerConfig(
        coin="#20",
        expiry_ts=2_000_000_000,
        quote_size_shares=1.0,
        min_spread_bps=30.0,
        max_position_shares=20.0,
        max_inventory_usd=5.0,
        refresh_interval_s=0.0,
    )
    m = _maker(base_cfg, market_meta_mk, journal)
    m._inventory_shares = 10.0  # GLFT skew active
    base_bid, base_ask = m._compute_quotes(0.05, 0.04, 0.06, sigma_eff=0.03)

    off_cfg = MakerConfig(**{**base_cfg.__dict__})  # identical, noskew off
    m2 = _maker(off_cfg, market_meta_mk, journal)
    m2._inventory_shares = 10.0
    bid, ask = m2._compute_quotes(0.05, 0.04, 0.06, sigma_eff=0.03)
    assert (bid, ask) == (base_bid, base_ask)


def test_noskew_yes_leg_shifts_quotes_down(market_meta_mk, journal):
    """YES leg, p=0.05 (steep band) → both quotes shifted DOWN vs baseline."""
    off = _maker(_noskew_cfg(noskew_enabled=False), market_meta_mk, journal)
    off._inventory_shares = 5.0  # keep ask side active (no-shorts guard)
    b0, a0 = off._compute_quotes(0.05, 0.04, 0.06, sigma_eff=0.0)
    on = _maker(_noskew_cfg(), market_meta_mk, journal)  # "#20" → YES leg
    on._inventory_shares = 5.0
    b1, a1 = on._compute_quotes(0.05, 0.04, 0.06, sigma_eff=0.0)
    assert b1 < b0
    assert a1 < a0


def test_noskew_no_leg_shifts_quotes_up(market_meta_mk, journal):
    """NO leg, YES-prob in band → both quotes shifted UP (buy NO eagerly)."""
    # "#21" is a NO leg; mid=0.95 → p = 1-0.95 = 0.05 (steep band). Hold
    # inventory so the ask side stays active (no-shorts guard otherwise mutes it).
    off = _maker(_noskew_cfg(coin="#21", noskew_enabled=False), market_meta_mk, journal)
    off._inventory_shares = 5.0
    b0, a0 = off._compute_quotes(0.95, 0.94, 0.96, sigma_eff=0.0)
    on = _maker(_noskew_cfg(coin="#21"), market_meta_mk, journal)
    on._inventory_shares = 5.0
    b1, a1 = on._compute_quotes(0.95, 0.94, 0.96, sigma_eff=0.0)
    assert b1 > b0
    assert a1 > a0


def test_noskew_bps_graded_and_zero_outside_band(market_meta_mk, journal):
    """bps graded: at p=0.05 > at p=0.30 > 0; p=0.60 and p=0.01 → zero.

    bps is the graded quantity. The price-space shift is bps/1e4 * mid, so it's
    also nonzero in-band and zero out-of-band (verified below)."""
    m = _maker(_noskew_cfg(), market_meta_mk, journal)
    assert m._noskew_bps(0.05) > m._noskew_bps(0.30) > 0
    # In-band shifts are nonzero; the steep-band shift exceeds the shallow band
    # at a common mid (compare magnitudes directly via _noskew_bps grading).
    assert abs(m._noskew_shift(0.05)) > 0
    assert abs(m._noskew_shift(0.30)) > 0
    assert m._noskew_bps(0.60) == 0.0
    assert m._noskew_bps(0.01) == 0.0
    assert m._noskew_shift(0.60) == 0.0
    assert m._noskew_shift(0.01) == 0.0
    # Continuity at the knee (p=0.10) and anchor values.
    assert m._noskew_bps(0.02) == pytest.approx(250.0)
    assert m._noskew_bps(0.10) == pytest.approx(100.0)
    assert m._noskew_bps(0.50) == pytest.approx(0.0)


def test_noskew_circuit_breaker_neutralizes_on_surge(market_meta_mk, journal):
    """A doubled p (vs rolling min) neutralizes the skew + journals the event."""
    m = _maker(_noskew_cfg(coin="#20", noskew_surge_mult=2.0), market_meta_mk, journal)
    # Seed a low baseline p (YES leg → p == mid).
    for x in [0.04, 0.045, 0.04, 0.05]:
        m._p_hist.append(x)
    # Sanity: without a surge the shift is nonzero at p=0.06.
    assert m._noskew_shift(0.06) != 0.0
    # p=0.10 is >= 2.0 * min(0.04) → surge → neutralized.
    assert m._noskew_shift(0.10) == 0.0
    events = _journal_events(journal)
    assert any(e["event"] == "noskew_circuit_breaker" for e in events)


def test_noskew_composition_clamped_under_long_inventory(market_meta_mk, journal):
    """NO-skew + GLFT skew compose additively; combined offset clamped to cap*mid.

    Use a pathological cap strength so the clamp binds, and assert no cross."""
    cfg = _noskew_cfg(
        coin="#20",
        combined_skew_cap_frac=0.0010,  # tight 10 bps cap → clamp must bind
        noskew_bps_at_floor=900.0,      # exaggerated edge to force the clamp
        noskew_bps_at_knee=900.0,
    )
    m = _maker(cfg, market_meta_mk, journal)
    m._inventory_shares = 18.0  # strong GLFT down-skew
    m._inventory_cost = 0.0
    mid, bb, ba = 0.05, 0.04, 0.06
    bid, ask = m._compute_quotes(mid, bb, ba, sigma_eff=0.05)
    # Sides may be suppressed by caps; if present they must not cross.
    if bid is not None and ask is not None:
        assert bid < ask
    # The directional offset of the pair is clamped: measure off the ask side,
    # which carries (-(offset_tick) - skew + fv_shift). Reconstruct the bound:
    cap = cfg.combined_skew_cap_frac * mid
    # The combined skew_total magnitude must not exceed cap (allow tick round).
    # ask_target = ba - tick - skew + fv_shift ; the clamp guarantees
    # |(-skew + fv_shift)| <= cap. Recover it from the un-rounded relation via
    # the public path: re-run with edge OFF (pure GLFT) and ON, diff is fv part.
    # Simpler: assert the realized ask shift from (ba - tick) is within cap.
    tick = 1e-5 * cfg.quote_offset_ticks
    if ask is not None:
        realized = (ba - tick) - ask  # = skew - fv_shift (the clamped total)
        assert abs(realized) <= cap + 1e-9


# ---------- toxicity / flow-imbalance defense ----------
#
# A burst of one-sided informed flow can reprice a leg faster than the 2s poll
# can cancel → adverse selection. The defense watches three signals: trade-flow
# imbalance (TFI, off the public trades tape), queue imbalance (QI) and depth
# evaporation (both off the l2Book). Actions: widen+cancel the hit side, or pull
# both sides and stand down. All OFF by default (byte-identical legacy behavior).


def _tox_cfg(**overrides):
    base = dict(
        coin="#20",
        expiry_ts=2_000_000_000,
        quote_size_shares=1.0,
        min_spread_bps=30.0,
        max_position_shares=20.0,
        max_inventory_usd=5.0,
        refresh_interval_s=0.0,
        cancel_threshold_bps=5.0,
        toxicity_enabled=True,
        tfi_window_s=10.0,
        tfi_window_trades=50,
        tfi_min_tape_sz=5.0,
        tfi_min_trades=3,
        tfi_cancel=0.6,
        tfi_pull=0.8,
        qi_confirm=0.6,
        depth_evap_frac=0.50,
        toxicity_widen_ticks=3,
        standdown_s=45.0,
    )
    base.update(overrides)
    return MakerConfig(**base)


def _trades_ws(trades):
    return {"data": trades}


def _trade(tid, side, sz, px=0.50, ts_ms=1_700_000_000_000, coin="#20"):
    return {"tid": tid, "coin": coin, "side": side, "sz": str(sz), "px": str(px), "time": ts_ms}


# --- handle_ws_trades: unwrap / filter / parse / dedupe ---


def test_ws_trades_unwrap_and_parse(mk_cfg, market_meta_mk, journal):
    m = _maker(mk_cfg, market_meta_mk, journal)
    m.handle_ws_trades(_trades_ws([_trade(1, "B", 2.0, 0.5, 1_700_000_000_000)]))
    assert len(m._tape) == 1
    t = m._tape[0]
    assert t["side"] == "B" and t["sz"] == 2.0 and t["px"] == 0.5
    assert t["ts"] == 1_700_000_000.0  # ms → s


def test_ws_trades_filters_foreign_coin(mk_cfg, market_meta_mk, journal):
    m = _maker(mk_cfg, market_meta_mk, journal)
    m.handle_ws_trades(_trades_ws([_trade(1, "B", 2.0, coin="#99")]))
    assert len(m._tape) == 0


def test_ws_trades_deduped_by_tid(mk_cfg, market_meta_mk, journal):
    m = _maker(mk_cfg, market_meta_mk, journal)
    m.handle_ws_trades(_trades_ws([_trade(7, "A", 3.0)]))
    m.handle_ws_trades(_trades_ws([_trade(7, "A", 3.0)]))  # replay
    assert len(m._tape) == 1


def test_ws_trades_drops_malformed(mk_cfg, market_meta_mk, journal):
    m = _maker(mk_cfg, market_meta_mk, journal)
    m.handle_ws_trades(None)
    m.handle_ws_trades({})
    m.handle_ws_trades(_trades_ws(["nope", 42]))
    m.handle_ws_trades(_trades_ws([{"tid": 1, "coin": "#20", "side": "X", "sz": "1", "px": "0.5", "time": 1}]))
    m.handle_ws_trades(_trades_ws([{"tid": 2, "coin": "#20", "side": "B", "sz": "bad", "px": "0.5", "time": 1}]))
    m.handle_ws_trades(_trades_ws([{"tid": 3, "coin": "#20", "side": "B", "sz": "1", "px": "0.5"}]))  # no time
    assert len(m._tape) == 0


def test_seen_trade_tids_disjoint_from_seen_tids(mk_cfg, market_meta_mk, journal):
    """Trade-tid dedupe set is SEPARATE from the fills-tid dedupe set."""
    m = _maker(mk_cfg, market_meta_mk, journal)
    m.handle_ws_trades(_trades_ws([_trade(100, "B", 1.0)]))
    assert 100 in m._seen_trade_tids
    assert 100 not in m._seen_tids
    # A fill with the SAME tid must still register independently.
    m.handle_ws_fills({"data": {"fills": [{"tid": 100, "coin": "#20", "px": "0.5", "sz": "1", "side": "B"}]}})
    assert 100 in m._seen_tids


# --- TFI ---


def test_tfi_all_buy_is_plus_one(mk_cfg, market_meta_mk, journal):
    m = _maker(mk_cfg, market_meta_mk, journal)
    for i in range(4):
        m.handle_ws_trades(_trades_ws([_trade(i, "B", 3.0)]))
    tfi, pressure = m._compute_tfi(1_700_000_001.0)
    assert tfi == pytest.approx(1.0)
    assert pressure == "B"


def test_tfi_all_sell_is_minus_one(mk_cfg, market_meta_mk, journal):
    m = _maker(mk_cfg, market_meta_mk, journal)
    for i in range(4):
        m.handle_ws_trades(_trades_ws([_trade(i, "A", 3.0)]))
    tfi, pressure = m._compute_tfi(1_700_000_001.0)
    assert tfi == pytest.approx(-1.0)
    assert pressure == "A"


def test_tfi_time_window_prunes_old_trades(mk_cfg, market_meta_mk, journal):
    cfg = _tox_cfg(tfi_window_s=10.0)
    m = _maker(cfg, market_meta_mk, journal)
    # Old buys (t=0s), recent sells (t=100s); window=10s drops the old buys.
    for i in range(4):
        m.handle_ws_trades(_trades_ws([_trade(i, "B", 3.0, ts_ms=1_700_000_000_000)]))
    for i in range(4):
        m.handle_ws_trades(_trades_ws([_trade(100 + i, "A", 3.0, ts_ms=1_700_000_100_000)]))
    tfi, pressure = m._compute_tfi(1_700_000_100.0 + 1)
    assert tfi == pytest.approx(-1.0)  # only the recent sells survive
    assert pressure == "A"


def test_tfi_count_window_prunes(mk_cfg, market_meta_mk, journal):
    cfg = _tox_cfg(tfi_window_trades=3, tfi_min_trades=1)
    m = _maker(cfg, market_meta_mk, journal)
    m.handle_ws_trades(_trades_ws([_trade(0, "B", 10.0)]))  # would dominate, but pruned
    for i in range(3):
        m.handle_ws_trades(_trades_ws([_trade(1 + i, "A", 3.0)]))
    tfi, _ = m._compute_tfi(1_700_000_001.0)
    assert tfi == pytest.approx(-1.0)  # only last 3 (all sells) kept


def test_tfi_min_tape_guard_returns_zero(mk_cfg, market_meta_mk, journal):
    """A tiny print below tfi_min_tape_sz → TFI inert (0.0, None)."""
    cfg = _tox_cfg(tfi_min_tape_sz=5.0, tfi_min_trades=3)
    m = _maker(cfg, market_meta_mk, journal)
    m.handle_ws_trades(_trades_ws([_trade(0, "B", 0.1)]))
    tfi, pressure = m._compute_tfi(1_700_000_001.0)
    assert tfi == 0.0 and pressure is None


def test_tfi_empty_tape_is_inert(mk_cfg, market_meta_mk, journal):
    m = _maker(mk_cfg, market_meta_mk, journal)
    assert m._compute_tfi(1_700_000_001.0) == (0.0, None)


# --- QI ---


def test_qi_sign(mk_cfg, market_meta_mk, journal):
    m = _maker(mk_cfg, market_meta_mk, journal)
    assert m._compute_qi(80, 20) == pytest.approx(0.6)  # bid-heavy
    assert m._compute_qi(20, 80) == pytest.approx(-0.6)  # ask-heavy
    assert m._compute_qi(0, 0) == 0.0  # degenerate


def test_qi_uses_external_size_own_stripped(mk_cfg, market_meta_mk, journal):
    """QI must read the OWN-STRIPPED external size, not our resting size."""
    m = _maker(mk_cfg, market_meta_mk, journal)
    # Our bid rests at 0.50 with our quote_size of 1.0; external bid size is 4.0.
    m._open.bid_oid = 999
    m._open.bid_px = 0.50
    bids = m._strip_own_level([{"px": "0.50", "sz": "5.0"}], m._open.bid_oid, m._open.bid_px)
    ext_bid_sz = float(bids[0]["sz"])
    assert ext_bid_sz == pytest.approx(4.0)  # 5.0 - 1.0 own
    assert m._compute_qi(ext_bid_sz, 4.0) == pytest.approx(0.0)


# --- depth evaporation ---


def test_depth_evap_one_side(mk_cfg, market_meta_mk, journal):
    cfg = _tox_cfg(depth_evap_frac=0.50)
    m = _maker(cfg, market_meta_mk, journal)
    last = {"bid_px": 0.50, "bid_sz": 100.0, "ask_px": 0.55, "ask_sz": 100.0}
    cur = {"bid_px": 0.50, "bid_sz": 100.0, "ask_px": 0.55, "ask_sz": 40.0}  # 60% gone
    bid_evap, ask_evap = m._compute_depth_evap(last, cur)
    assert ask_evap is True and bid_evap is False


def test_depth_evap_none_on_cold_start(mk_cfg, market_meta_mk, journal):
    m = _maker(mk_cfg, market_meta_mk, journal)
    cur = {"bid_px": 0.50, "bid_sz": 1.0, "ask_px": 0.55, "ask_sz": 1.0}
    assert m._compute_depth_evap(None, cur) == (False, False)


# --- tick() integration ---


def _drive_tape_buy(m, n=4, sz=3.0, base_tid=0, ts_ms=1_700_000_000_000):
    for i in range(n):
        m.handle_ws_trades(_trades_ws([_trade(base_tid + i, "B", sz, ts_ms=ts_ms)]))


def _drive_tape_sell(m, n=4, sz=3.0, base_tid=0, ts_ms=1_700_000_000_000):
    for i in range(n):
        m.handle_ws_trades(_trades_ws([_trade(base_tid + i, "A", sz, ts_ms=ts_ms)]))


def test_tick_tfi_cancel_widens_hit_side(mk_cfg, market_meta_mk, journal, monkeypatch):
    """|TFI| >= tfi_cancel (buy pressure) → cancel+widen the ASK (hit side)."""
    import time as _time

    cfg = _tox_cfg(tfi_cancel=0.6, tfi_pull=0.8, qi_confirm=2.0)  # qi can't escalate
    m = _maker(cfg, market_meta_mk, journal)
    m._inventory_shares = 5.0  # so ask can quote
    m._inventory_cost = 2.0
    monkeypatch.setattr(_time, "time", lambda: 1_700_000_000.0 + 1)
    # Mostly-buy tape → TFI ≈ +0.71 (in [tfi_cancel 0.6, tfi_pull 0.8)) → cancel-side,
    # hit = ASK. (An ALL-buy tape gives TFI=+1.0 ≥ tfi_pull and triggers a full pull —
    # that path is covered by test_tick_tfi_pull_sets_standdown; here we isolate the
    # cancel_side band.)
    _drive_tape_buy(m, n=4, sz=3.0)      # buy vol 12
    _drive_tape_sell(m, n=1, sz=2.0, base_tid=4)  # sell vol 2 → TFI=(12-2)/(12+2)=0.714
    m.info.post.return_value = _book(0.50, 0.55, bid_sz=50, ask_sz=50)  # QI ~ 0
    m.tick()
    events = _journal_events(journal)
    tox = [e for e in events if e["event"] == "maker_toxicity"]
    assert tox and tox[-1]["action"] == "cancel_side" and tox[-1]["hit"] == "A"
    # Ask was widened: it sits further below best_ask than the bid sits above
    # best_bid (symmetric base offset + 3 extra ticks on the ask only).
    assert m._open.ask_oid is not None
    assert (0.55 - m._open.ask_px) > (m._open.bid_px - 0.50)


def test_tick_tfi_pull_sets_standdown(mk_cfg, market_meta_mk, journal, monkeypatch):
    """|TFI| >= tfi_pull → pull BOTH sides and arm the stand-down."""
    import time as _time

    cfg = _tox_cfg(tfi_pull=0.8, standdown_s=45.0)
    m = _maker(cfg, market_meta_mk, journal)
    now = 1_700_000_000.0 + 1
    monkeypatch.setattr(_time, "time", lambda: now)
    _drive_tape_buy(m, n=4, sz=3.0)  # TFI = +1 >= 0.8 → PULL
    m.info.post.return_value = _book(0.50, 0.55, bid_sz=50, ask_sz=50)
    m.tick()
    assert m._open.bid_oid is None and m._open.ask_oid is None
    assert m._standdown_until == pytest.approx(now + 45.0)
    events = _journal_events(journal)
    assert any(e["event"] == "maker_toxicity" and e["action"] == "pull" for e in events)


def test_standdown_gate_returns_before_book_fetch(mk_cfg, market_meta_mk, journal, monkeypatch):
    import time as _time

    cfg = _tox_cfg()
    m = _maker(cfg, market_meta_mk, journal)
    now = 1_700_000_000.0
    m._standdown_until = now + 30.0
    monkeypatch.setattr(_time, "time", lambda: now)
    m.info.post.return_value = _book(0.50, 0.55)
    m.tick()
    m.info.post.assert_not_called()  # never fetched the book
    assert m._open.bid_oid is None
    events = _journal_events(journal)
    assert any(e["event"] == "maker_skip" and e["reason"] == "standdown" for e in events)


def test_qi_confirm_escalates_cancel_to_pull(mk_cfg, market_meta_mk, journal, monkeypatch):
    """|TFI| in [cancel, pull) but QI confirms past qi_confirm → escalate to PULL."""
    import time as _time

    cfg = _tox_cfg(tfi_cancel=0.6, tfi_pull=0.95, qi_confirm=0.6)
    m = _maker(cfg, market_meta_mk, journal)
    monkeypatch.setattr(_time, "time", lambda: 1_700_000_000.0 + 1)
    # 3 buys + 1 sell → TFI = (9-3)/12 = 0.5? choose 4 buys 1 sell: (12-3)/15=0.6
    _drive_tape_buy(m, n=4, sz=3.0, base_tid=0)
    m.handle_ws_trades(_trades_ws([_trade(50, "A", 3.0)]))  # TFI = 9/15 = 0.6
    # Bid-heavy book confirms buy pressure: QI = (90-10)/100 = 0.8 >= 0.6.
    m.info.post.return_value = _book(0.50, 0.55, bid_sz=90, ask_sz=10)
    m.tick()
    assert m._open.bid_oid is None and m._open.ask_oid is None  # pulled
    events = _journal_events(journal)
    assert any(e["event"] == "maker_toxicity" and e["action"] == "pull" for e in events)


def test_both_side_evaporation_pulls(mk_cfg, market_meta_mk, journal, monkeypatch):
    import time as _time

    cfg = _tox_cfg(depth_evap_frac=0.50)
    m = _maker(cfg, market_meta_mk, journal)
    t0 = 1_700_000_000.0
    monkeypatch.setattr(_time, "time", lambda: t0)
    # First tick: deep book, no prior top → records _last_book_top.
    m.info.post.return_value = _book(0.50, 0.55, bid_sz=100, ask_sz=100)
    m.tick()
    assert m._last_book_top is not None
    # Second tick: both sides collapse > 50% → PULL (no tape needed).
    m.info.post.return_value = _book(0.50, 0.55, bid_sz=20, ask_sz=20)
    m.tick()
    assert m._open.bid_oid is None and m._open.ask_oid is None
    assert m._standdown_until > t0
    events = _journal_events(journal)
    assert any(e["event"] == "maker_toxicity" and e["action"] == "pull" for e in events)


def test_widen_composes_with_glft_skew(mk_cfg, market_meta_mk, journal):
    """The widen offset is ADDITIVE on top of the GLFT inventory skew."""
    cfg = _tox_cfg(use_glft_skew=True, toxicity_widen_ticks=3)
    m = _maker(cfg, market_meta_mk, journal)
    m._inventory_shares = 5.0
    m._inventory_cost = 2.0
    mid, bb, ba = 0.525, 0.50, 0.55
    base_bid, base_ask = m._compute_quotes(mid, bb, ba, sigma_eff=0.03)
    wide_bid, wide_ask = m._compute_quotes(mid, bb, ba, sigma_eff=0.03, widen_ask=3)
    # Bid unchanged (same skew); ask pushed 3 ticks further down vs the base.
    assert wide_bid == pytest.approx(base_bid)
    assert base_ask - wide_ask == pytest.approx(3e-5, abs=1e-9)


def test_toxicity_disabled_is_fully_inert(mk_cfg, market_meta_mk, journal, monkeypatch):
    """toxicity_enabled=False → no standdown gate, no eval, no _last_book_top."""
    import time as _time

    cfg = _tox_cfg(toxicity_enabled=False)
    m = _maker(cfg, market_meta_mk, journal)
    monkeypatch.setattr(_time, "time", lambda: 1_700_000_000.0 + 1)
    # Heavy one-sided tape that WOULD pull if enabled.
    _drive_tape_buy(m, n=6, sz=5.0)
    m._standdown_until = 1_700_000_000.0 + 999  # would gate if checked
    m.info.post.return_value = _book(0.50, 0.55, bid_sz=50, ask_sz=50)
    m.tick()
    # Standdown ignored (book fetched, normal quote), no toxicity event.
    m.info.post.assert_called()
    assert m._open.bid_oid is not None
    assert m._last_book_top is None
    events = _journal_events(journal)
    assert not any(e["event"] == "maker_toxicity" for e in events)


# --- live account-safety gate (red-team 2026-07-07: master-account mislaunch) ---
import pytest as _pytest
from src.maker import _assert_live_account_safety


def _safe_kwargs(**over):
    d = dict(dry_run=False, arg_account="0xSUB", cfg_account="0xMASTER",
             signer_addr="0xAGENT", agent_addrs=["0xagent"], kill_file="./KILL.maker",
             cfg_kill_file="./KILL")
    d.update(over)
    return d


def test_live_safety_ok_for_valid_subaccount():
    _assert_live_account_safety(**_safe_kwargs())  # must not raise


def test_dry_run_exempt():
    _assert_live_account_safety(**_safe_kwargs(dry_run=True, arg_account=None,
                                               agent_addrs=[], kill_file="./KILL"))


def test_live_refuses_missing_account():
    with _pytest.raises(SystemExit):
        _assert_live_account_safety(**_safe_kwargs(arg_account=None))


def test_live_refuses_master_account():
    with _pytest.raises(SystemExit):
        _assert_live_account_safety(**_safe_kwargs(arg_account="0xMASTER"))


def test_live_refuses_unauthorized_key():
    with _pytest.raises(SystemExit):
        _assert_live_account_safety(**_safe_kwargs(signer_addr="0xWRONGKEY", agent_addrs=["0xagent"]))


def test_live_refuses_shared_kill_file():
    with _pytest.raises(SystemExit):
        _assert_live_account_safety(**_safe_kwargs(kill_file="./KILL", cfg_kill_file="./KILL"))
