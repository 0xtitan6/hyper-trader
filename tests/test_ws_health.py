"""Tests for src/ws_health.py — WS subscription tracking + rebuild on failure."""

from __future__ import annotations

import time
from unittest.mock import MagicMock, patch

from src.ws_health import WSHealthMonitor


def _make_info(thread_alive: bool = True, ws_ready: bool = True) -> MagicMock:
    """Build a mock Info with an attached ws_manager."""
    info = MagicMock()
    info.base_url = "https://api.hyperliquid.xyz"
    ws_mgr = MagicMock()
    ws_mgr.is_alive.return_value = thread_alive
    ws_mgr.ws_ready = ws_ready
    ws_mgr.subscribe.return_value = 1  # subscription_id
    info.ws_manager = ws_mgr
    return info


def test_subscribe_tracks_pair_and_delegates_to_sdk():
    info = _make_info()
    mon = WSHealthMonitor(info, stale_threshold_s=10)
    cb = MagicMock()
    sub = {"type": "userFills", "user": "0xabc"}
    sid = mon.subscribe(sub, cb)
    # Tracked locally
    assert len(mon._subs) == 1
    assert mon._subs[0][0] == sub
    # Delegated to SDK's manager
    info.ws_manager.subscribe.assert_called_once()
    args = info.ws_manager.subscribe.call_args
    assert args.args[0] == sub
    # Callback passed to SDK is our wrapper, not user's raw callback
    wrapped = args.args[1]
    assert wrapped is not cb
    assert sid == 1


def test_wrapped_callback_touches_freshness_and_forwards():
    info = _make_info()
    mon = WSHealthMonitor(info)
    cb = MagicMock()
    mon.subscribe({"type": "userFills", "user": "0xabc"}, cb)
    # Grab the wrapper that was registered with the SDK
    wrapped = info.ws_manager.subscribe.call_args.args[1]
    # Set last_msg_ts far back, then fire — should reset
    mon._last_msg_ts = time.time() - 1000
    wrapped({"channel": "userFills", "data": {"fills": []}})
    assert mon.last_msg_age_s() < 1.0
    cb.assert_called_once()


def test_wrapped_callback_swallows_user_exception():
    info = _make_info()
    mon = WSHealthMonitor(info)
    cb = MagicMock(side_effect=RuntimeError("boom"))
    mon.subscribe({"type": "userFills", "user": "0xabc"}, cb)
    wrapped = info.ws_manager.subscribe.call_args.args[1]
    # Must not raise
    wrapped({"channel": "userFills", "data": {}})
    cb.assert_called_once()


def test_healthy_no_rebuild():
    info = _make_info(thread_alive=True, ws_ready=True)
    alerter = MagicMock()
    mon = WSHealthMonitor(info, alerter=alerter, stale_threshold_s=10)
    mon.subscribe({"type": "userFills", "user": "0xabc"}, MagicMock())
    # Run one check — should not rebuild
    with patch("src.ws_health.WSHealthMonitor._rebuild") as mock_rebuild:
        mon._check_and_recover()
    mock_rebuild.assert_not_called()


def test_unhealthy_thread_dead_triggers_rebuild():
    info = _make_info(thread_alive=False, ws_ready=True)
    alerter = MagicMock()
    mon = WSHealthMonitor(info, alerter=alerter, stale_threshold_s=10)
    mon.subscribe({"type": "userFills", "user": "0xabc"}, MagicMock())
    with patch("src.ws_health.WSHealthMonitor._rebuild") as mock_rebuild:
        mon._check_and_recover()
    mock_rebuild.assert_called_once()
    alerter.alert.assert_called()  # warn alert about rebuild


def test_unhealthy_ws_not_ready_triggers_rebuild():
    info = _make_info(thread_alive=True, ws_ready=False)
    alerter = MagicMock()
    mon = WSHealthMonitor(info, alerter=alerter, stale_threshold_s=10)
    mon.subscribe({"type": "userFills", "user": "0xabc"}, MagicMock())
    with patch("src.ws_health.WSHealthMonitor._rebuild") as mock_rebuild:
        mon._check_and_recover()
    mock_rebuild.assert_called_once()


def test_silent_stale_triggers_rebuild():
    """ws_ready=True but no messages received for stale_threshold_s — the bug."""
    info = _make_info(thread_alive=True, ws_ready=True)
    alerter = MagicMock()
    mon = WSHealthMonitor(info, alerter=alerter, stale_threshold_s=10)
    mon.subscribe({"type": "userFills", "user": "0xabc"}, MagicMock())
    # Force last_msg_ts into the past
    mon._last_msg_ts = time.time() - 60
    with patch("src.ws_health.WSHealthMonitor._rebuild") as mock_rebuild:
        mon._check_and_recover()
    mock_rebuild.assert_called_once()


def test_stale_with_no_subs_does_not_trigger():
    """Before any subscription, staleness is meaningless — don't rebuild."""
    info = _make_info(thread_alive=True, ws_ready=True)
    alerter = MagicMock()
    mon = WSHealthMonitor(info, alerter=alerter, stale_threshold_s=10)
    mon._last_msg_ts = time.time() - 60  # ancient
    with patch("src.ws_health.WSHealthMonitor._rebuild") as mock_rebuild:
        mon._check_and_recover()
    mock_rebuild.assert_not_called()


def test_rebuild_replays_subs_to_new_manager():
    """The critical fix: after rebuild, every tracked sub must be re-registered."""
    info = _make_info(thread_alive=False, ws_ready=False)
    alerter = MagicMock()
    mon = WSHealthMonitor(info, alerter=alerter, rebuild_ready_wait_s=0.0)

    sub1 = {"type": "userFills", "user": "0xabc"}
    sub2 = {"type": "userFills", "user": "0xdef"}
    cb1, cb2 = MagicMock(), MagicMock()
    mon.subscribe(sub1, cb1)
    mon.subscribe(sub2, cb2)

    old_mgr = info.ws_manager
    new_mgr = MagicMock()
    new_mgr.ws_ready = True

    with patch("hyperliquid.websocket_manager.WebsocketManager", return_value=new_mgr) as mock_cls:
        mon._rebuild()

    # Old manager torn down
    old_mgr.stop.assert_called_once()
    # New manager constructed with same base_url
    mock_cls.assert_called_once_with(info.base_url)
    new_mgr.start.assert_called_once()
    # Both subs replayed against the new manager
    assert new_mgr.subscribe.call_count == 2
    replayed_subs = [call.args[0] for call in new_mgr.subscribe.call_args_list]
    assert sub1 in replayed_subs
    assert sub2 in replayed_subs
    # Info.ws_manager now points at the new manager
    assert info.ws_manager is new_mgr
    # Rebuild counter incremented
    assert mon.rebuild_count == 1


def test_rebuild_continues_when_old_stop_fails():
    """A broken old manager (stop raises) shouldn't block the new one."""
    info = _make_info(thread_alive=False)
    info.ws_manager.stop.side_effect = RuntimeError("teardown failed")
    mon = WSHealthMonitor(info, rebuild_ready_wait_s=0.0)
    mon.subscribe({"type": "userFills", "user": "0xabc"}, MagicMock())

    new_mgr = MagicMock()
    new_mgr.ws_ready = True
    with patch("hyperliquid.websocket_manager.WebsocketManager", return_value=new_mgr):
        mon._rebuild()  # must not raise
    new_mgr.subscribe.assert_called_once()


def test_rebuild_continues_when_replay_subscribe_fails():
    """If replaying one sub raises, the others still go through."""
    info = _make_info(thread_alive=False)
    mon = WSHealthMonitor(info, rebuild_ready_wait_s=0.0)
    sub1 = {"type": "userFills", "user": "0xabc"}
    sub2 = {"type": "userFills", "user": "0xdef"}
    mon.subscribe(sub1, MagicMock())
    mon.subscribe(sub2, MagicMock())

    new_mgr = MagicMock()
    new_mgr.ws_ready = True
    # Make the FIRST subscribe call on the new manager raise
    new_mgr.subscribe.side_effect = [RuntimeError("first fails"), 99]
    with patch("hyperliquid.websocket_manager.WebsocketManager", return_value=new_mgr):
        mon._rebuild()
    assert new_mgr.subscribe.call_count == 2
    # Rebuild still completed (counter incremented)
    assert mon.rebuild_count == 1


def test_subscribe_raises_when_skip_ws():
    """If Info was constructed with skip_ws=True, ws_manager is None — fail loudly."""
    info = MagicMock()
    info.ws_manager = None
    mon = WSHealthMonitor(info)
    try:
        mon.subscribe({"type": "userFills", "user": "0xabc"}, MagicMock())
        raise AssertionError("expected RuntimeError")
    except RuntimeError as e:
        assert "skip_ws" in str(e)


def test_rebuild_resets_last_msg_ts():
    """After rebuild, the freshness clock resets so we don't immediately
    re-trigger another rebuild while waiting for the new connection to receive."""
    info = _make_info(thread_alive=False)
    mon = WSHealthMonitor(info, rebuild_ready_wait_s=0.0)
    mon.subscribe({"type": "userFills", "user": "0xabc"}, MagicMock())
    mon._last_msg_ts = time.time() - 1000  # ancient

    new_mgr = MagicMock()
    new_mgr.ws_ready = True
    with patch("hyperliquid.websocket_manager.WebsocketManager", return_value=new_mgr):
        mon._rebuild()
    # After rebuild, freshness clock is recent
    assert mon.last_msg_age_s() < 1.0
