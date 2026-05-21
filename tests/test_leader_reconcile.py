"""Tests for src/leader_reconcile.py — leader exit detection + auto-close."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from src.leader_reconcile import (
    STATUS_CLOSED,
    STATUS_FLIPPED,
    STATUS_OK,
    STATUS_ORPHAN,
    STATUS_UNKNOWN,
    LeaderReconciler,
)


# --- helpers ---------------------------------------------------------------


def _mock_info(leader_books: dict[str, dict[str, float]]) -> MagicMock:
    """Build an Info mock whose user_state(addr) returns asset positions for
    that address as if from HL. `leader_books` is {address_lower: {coin: szi}}.
    """
    info = MagicMock()

    def user_state(address: str) -> dict:
        book = leader_books.get(address.lower(), {})
        return {
            "assetPositions": [
                {"position": {"coin": c, "szi": str(s)}} for c, s in book.items()
            ]
        }

    info.user_state.side_effect = user_state
    info.all_mids.return_value = {"BTC": "60000.0", "ZEC": "650.0", "TAO": "275.0"}
    return info


def _make_reconciler(
    state,
    journal,
    leader_books: dict[str, dict[str, float]],
    *,
    auto_close: bool = False,
    debounce: int = 2,
    exchange: MagicMock | None = None,
    market_meta=None,
) -> LeaderReconciler:
    return LeaderReconciler(
        info=_mock_info(leader_books),
        state=state,
        journal=journal,
        alerter=MagicMock(),
        exchange=exchange,
        market_meta=market_meta,
        auto_close=auto_close,
        debounce_cycles=debounce,
        slippage_bps=50.0,
    )


# --- classification --------------------------------------------------------


def test_ok_when_originator_still_short(state, journal):
    state.update_position("BTC", -0.5, 60000.0)
    state.set_position_originator("BTC", "0xABC")
    r = _make_reconciler(state, journal, {"0xabc": {"BTC": -0.5}})
    status = r.reconcile(["0xABC"])
    assert status["BTC"] == STATUS_OK


def test_closed_when_originator_flat(state, journal):
    state.update_position("BTC", -0.5, 60000.0)
    state.set_position_originator("BTC", "0xABC")
    r = _make_reconciler(state, journal, {"0xabc": {}})
    status = r.reconcile(["0xABC"])
    assert status["BTC"] == STATUS_CLOSED


def test_flipped_when_originator_reverses(state, journal):
    state.update_position("BTC", -0.5, 60000.0)
    state.set_position_originator("BTC", "0xABC")
    # Originator now LONG
    r = _make_reconciler(state, journal, {"0xabc": {"BTC": 1.0}})
    status = r.reconcile(["0xABC"])
    assert status["BTC"] == STATUS_FLIPPED


def test_orphan_when_no_originator_and_no_follower_holds_same_dir(state, journal):
    # Pre-PR-#25 position: originator unset
    state.update_position("BTC", -0.5, 60000.0)
    # None of the followed leaders is short BTC
    r = _make_reconciler(
        state, journal, {"0xabc": {"BTC": 1.0}, "0xdef": {"ETH": -2.0}}
    )
    status = r.reconcile(["0xABC", "0xDEF"])
    assert status["BTC"] == STATUS_ORPHAN


def test_orphan_recovers_to_ok_if_another_leader_holds_same_dir(state, journal):
    state.update_position("BTC", -0.5, 60000.0)
    # Originator unset, but 0xdef holds the short
    r = _make_reconciler(
        state, journal, {"0xabc": {"ETH": 1.0}, "0xdef": {"BTC": -2.0}}
    )
    status = r.reconcile(["0xABC", "0xDEF"])
    assert status["BTC"] == STATUS_OK


def test_zero_size_position_not_reported(state, journal):
    # A zeroed historical position should be ignored
    state.update_position("BTC", 0.0, 0.0)
    r = _make_reconciler(state, journal, {"0xabc": {}})
    status = r.reconcile(["0xABC"])
    assert "BTC" not in status


# --- debounce --------------------------------------------------------------


def test_debounce_holds_off_action_on_first_cycle(state, journal):
    state.update_position("BTC", -0.5, 60000.0)
    state.set_position_originator("BTC", "0xABC")
    r = _make_reconciler(state, journal, {"0xabc": {}}, auto_close=False, debounce=2)
    r.reconcile(["0xABC"])
    # First detection should not yet emit `leader_exit_detected`
    assert r.alerter.alert.call_count == 0  # no alert yet
    assert r._stale_counts["BTC"] == 1


def test_debounce_fires_action_on_second_consecutive_cycle(state, journal):
    state.update_position("BTC", -0.5, 60000.0)
    state.set_position_originator("BTC", "0xABC")
    r = _make_reconciler(state, journal, {"0xabc": {}}, auto_close=False, debounce=2)
    r.reconcile(["0xABC"])
    r.reconcile(["0xABC"])
    assert r.alerter.alert.call_count == 1
    # Counter reset after acting so we don't re-spam on cycle 3
    assert r._stale_counts.get("BTC", 0) == 0


def test_debounce_resets_on_recovery(state, journal):
    state.update_position("BTC", -0.5, 60000.0)
    state.set_position_originator("BTC", "0xABC")
    # Cycle 1: stale (originator flat)
    r = _make_reconciler(state, journal, {"0xabc": {}}, debounce=2)
    r.reconcile(["0xABC"])
    assert r._stale_counts["BTC"] == 1
    # Cycle 2: originator back to holding short — should reset counter
    r.info = _mock_info({"0xabc": {"BTC": -0.5}})
    r.reconcile(["0xABC"])
    assert "BTC" not in r._stale_counts
    assert r.alerter.alert.call_count == 0


# --- alert / journal behavior ----------------------------------------------


def test_stale_emits_alert_and_journal(state, journal):
    state.update_position("BTC", -0.5, 60000.0)
    state.set_position_originator("BTC", "0xABC")
    r = _make_reconciler(state, journal, {"0xabc": {}}, auto_close=False, debounce=1)
    r.reconcile(["0xABC"])
    r.alerter.alert.assert_called_once()
    level, msg = r.alerter.alert.call_args.args
    assert level == "error"
    assert "BTC" in msg
    assert "closed" in msg
    # journal entry written
    with open(journal.path) as f:
        lines = f.readlines()
    assert any("leader_exit_detected" in line for line in lines)


# --- auto-close ------------------------------------------------------------


def test_auto_close_submits_reduce_only_buy_for_short(state, journal, market_meta):
    state.update_position("BTC", -0.5, 60000.0)
    state.set_position_originator("BTC", "0xABC")
    exchange = MagicMock()
    exchange.order.return_value = {"status": "ok"}
    r = _make_reconciler(
        state,
        journal,
        {"0xabc": {}},
        auto_close=True,
        debounce=1,
        exchange=exchange,
        market_meta=market_meta,
    )
    r.reconcile(["0xABC"])
    exchange.order.assert_called_once()
    args = exchange.order.call_args
    # Positional: coin, is_buy, sz, limit_px
    assert args.args[0] == "BTC"
    assert args.args[1] is True  # BUY to close a short
    assert args.args[2] == 0.5  # abs of our short size
    assert args.kwargs["reduce_only"] is True
    assert args.kwargs["order_type"] == {"limit": {"tif": "Ioc"}}


def test_auto_close_submits_reduce_only_sell_for_long(state, journal, market_meta):
    state.update_position("ETH", 2.0, 3500.0)
    state.set_position_originator("ETH", "0xABC")
    exchange = MagicMock()
    exchange.order.return_value = {"status": "ok"}
    r = _make_reconciler(
        state,
        journal,
        {"0xabc": {}},
        auto_close=True,
        debounce=1,
        exchange=exchange,
        market_meta=market_meta,
    )
    info = r.info
    info.all_mids.return_value = {"ETH": "3700.0"}
    r.reconcile(["0xABC"])
    exchange.order.assert_called_once()
    args = exchange.order.call_args
    assert args.args[0] == "ETH"
    assert args.args[1] is False  # SELL to close a long
    assert args.args[2] == 2.0
    assert args.kwargs["reduce_only"] is True


def test_auto_close_disabled_does_not_call_exchange(state, journal, market_meta):
    state.update_position("BTC", -0.5, 60000.0)
    state.set_position_originator("BTC", "0xABC")
    exchange = MagicMock()
    r = _make_reconciler(
        state,
        journal,
        {"0xabc": {}},
        auto_close=False,
        debounce=1,
        exchange=exchange,
        market_meta=market_meta,
    )
    r.reconcile(["0xABC"])
    exchange.order.assert_not_called()


# --- failure handling ------------------------------------------------------


def test_unknown_when_originator_fetch_fails(state, journal):
    """If the originator's user_state errors but at least one other fetch
    succeeds, we don't act — wait for the next cycle."""
    state.update_position("BTC", -0.5, 60000.0)
    state.set_position_originator("BTC", "0xABC")
    info = MagicMock()

    def user_state(address: str) -> dict:
        if address.lower() == "0xabc":
            raise OSError("network down")
        return {"assetPositions": []}

    info.user_state.side_effect = user_state
    info.all_mids.return_value = {"BTC": "60000.0"}
    r = LeaderReconciler(
        info=info,
        state=state,
        journal=journal,
        alerter=MagicMock(),
        debounce_cycles=1,
    )
    status = r.reconcile(["0xABC", "0xDEF"])
    assert status["BTC"] == STATUS_UNKNOWN
    r.alerter.alert.assert_not_called()


def test_all_fetches_fail_returns_empty_no_action(state, journal):
    """When every leader fetch fails, we abort the cycle and act on nothing."""
    state.update_position("BTC", -0.5, 60000.0)
    state.set_position_originator("BTC", "0xABC")
    info = MagicMock()
    info.user_state.side_effect = OSError("network down")
    r = LeaderReconciler(
        info=info,
        state=state,
        journal=journal,
        alerter=MagicMock(),
        debounce_cycles=1,
    )
    status = r.reconcile(["0xABC"])
    assert status == {}
    r.alerter.alert.assert_not_called()


def test_auto_close_handles_order_exception(state, journal, market_meta):
    state.update_position("BTC", -0.5, 60000.0)
    state.set_position_originator("BTC", "0xABC")
    exchange = MagicMock()
    exchange.order.side_effect = RuntimeError("rejected")
    r = _make_reconciler(
        state,
        journal,
        {"0xabc": {}},
        auto_close=True,
        debounce=1,
        exchange=exchange,
        market_meta=market_meta,
    )
    # Should not raise out of reconcile()
    r.reconcile(["0xABC"])
    # Alert about the failure should be emitted
    assert any(
        "auto-close order failed" in str(call.args[1])
        for call in r.alerter.alert.call_args_list
    )


# --- reset utility ---------------------------------------------------------


def test_reset_debounce_all(state, journal):
    state.update_position("BTC", -0.5, 60000.0)
    state.set_position_originator("BTC", "0xABC")
    r = _make_reconciler(state, journal, {"0xabc": {}}, debounce=3)
    r.reconcile(["0xABC"])
    assert r._stale_counts["BTC"] == 1
    r.reset_debounce()
    assert r._stale_counts == {}


def test_reset_debounce_single_coin(state, journal):
    state.update_position("BTC", -0.5, 60000.0)
    state.update_position("ETH", 2.0, 3500.0)
    state.set_position_originator("BTC", "0xABC")
    state.set_position_originator("ETH", "0xABC")
    r = _make_reconciler(state, journal, {"0xabc": {}}, debounce=3)
    r.reconcile(["0xABC"])
    assert set(r._stale_counts) == {"BTC", "ETH"}
    r.reset_debounce("BTC")
    assert set(r._stale_counts) == {"ETH"}
