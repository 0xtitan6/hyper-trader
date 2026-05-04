from unittest.mock import MagicMock

from src.follower import FillFollower
from src.state import State


def _info_capturing_subscribe():
    info = MagicMock()
    captured: dict[str, object] = {}

    def subscribe(sub, cb):
        captured.setdefault("subs", []).append(sub)  # type: ignore[union-attr]
        captured.setdefault("cbs", []).append(cb)  # type: ignore[union-attr]

    info.subscribe.side_effect = subscribe
    return info, captured


def test_subscribes_once_per_address(state: State):
    info, _ = _info_capturing_subscribe()
    cb = MagicMock()
    f = FillFollower(info, cb, state)
    f.follow(["0xAAA", "0xbbb", "0xAAA"])
    assert info.subscribe.call_count == 2


def test_repeated_follow_no_resubscribe(state: State):
    info, _ = _info_capturing_subscribe()
    f = FillFollower(info, MagicMock(), state)
    f.follow(["0xa"])
    f.follow(["0xa", "0xb"])
    assert info.subscribe.call_count == 2


def test_snapshot_marks_seen_no_callback(state: State):
    info, captured = _info_capturing_subscribe()
    cb = MagicMock()
    f = FillFollower(info, cb, state)
    f.follow(["0xabc"])

    handler = captured["cbs"][0]  # type: ignore[index]
    handler(
        {
            "data": {
                "isSnapshot": True,
                "fills": [
                    {"tid": 1, "coin": "#11"},
                    {"tid": 2, "coin": "#11"},
                ],
            }
        }
    )
    cb.assert_not_called()
    assert state.is_tid_seen(1)
    assert state.is_tid_seen(2)


def test_new_fill_forwarded_once(state: State):
    info, captured = _info_capturing_subscribe()
    cb = MagicMock()
    f = FillFollower(info, cb, state)
    f.follow(["0xabc"])
    handler = captured["cbs"][0]  # type: ignore[index]
    handler({"data": {"isSnapshot": False, "fills": [{"tid": 7, "coin": "#11", "px": "0.5"}]}})
    cb.assert_called_once()
    addr, fill = cb.call_args.args
    assert addr == "0xabc"
    assert fill["tid"] == 7


def test_dedupe_across_messages(state: State):
    info, captured = _info_capturing_subscribe()
    cb = MagicMock()
    f = FillFollower(info, cb, state)
    f.follow(["0xabc"])
    handler = captured["cbs"][0]  # type: ignore[index]
    handler({"data": {"isSnapshot": False, "fills": [{"tid": 7}]}})
    handler({"data": {"isSnapshot": False, "fills": [{"tid": 7}]}})
    assert cb.call_count == 1


def test_health_touched_on_each_msg(state: State):
    info, captured = _info_capturing_subscribe()
    health = MagicMock()
    f = FillFollower(info, MagicMock(), state, health)
    f.follow(["0xabc"])
    handler = captured["cbs"][0]  # type: ignore[index]
    handler({"data": {"isSnapshot": False, "fills": []}})
    handler({"data": {"isSnapshot": False, "fills": []}})
    assert health.touch.call_count == 2


def test_malformed_message_does_not_raise(state: State):
    info, captured = _info_capturing_subscribe()
    cb = MagicMock()
    f = FillFollower(info, cb, state)
    f.follow(["0xabc"])
    handler = captured["cbs"][0]  # type: ignore[index]
    handler("not a dict")  # must not raise
    handler({"data": "not a dict either"})  # must not raise
    handler({"data": {"fills": [{"tid": None}]}})  # tid None ignored
    cb.assert_not_called()


def test_backfill_fetches_for_each_subscribed_leader(state: State):
    info, _ = _info_capturing_subscribe()
    info.post.return_value = []
    cb = MagicMock()
    f = FillFollower(info, cb, state)
    f.follow(["0xaaa", "0xbbb"])
    f.backfill(since_ms=1714000000000)
    # 2 leaders → 2 REST calls
    assert info.post.call_count == 2
    payloads = [c.args[1] for c in info.post.call_args_list]
    types = {p["type"] for p in payloads}
    users = {p["user"] for p in payloads}
    assert types == {"userFillsByTime"}
    assert users == {"0xaaa", "0xbbb"}


def test_backfill_dedupes_against_state(state: State):
    info, _ = _info_capturing_subscribe()
    info.post.return_value = [
        {"tid": 100, "coin": "#11", "px": "0.5", "sz": "10", "side": "B"},
        {"tid": 101, "coin": "#11", "px": "0.5", "sz": "10", "side": "A"},
    ]
    state.mark_tid_seen(100, "0xaaa")  # already seen
    cb = MagicMock()
    f = FillFollower(info, cb, state)
    f.follow(["0xaaa"])
    n = f.backfill(since_ms=0)
    # Only tid 101 is new
    assert n == 1
    assert cb.call_count == 1
    assert cb.call_args.args[1]["tid"] == 101


def test_backfill_handles_rest_failure_gracefully(state: State):
    info, _ = _info_capturing_subscribe()
    info.post.side_effect = RuntimeError("upstream down")
    cb = MagicMock()
    f = FillFollower(info, cb, state)
    f.follow(["0xaaa", "0xbbb"])
    n = f.backfill(since_ms=0)
    assert n == 0
    cb.assert_not_called()


def test_backfill_default_since_ms(state: State):
    info, _ = _info_capturing_subscribe()
    info.post.return_value = []
    cb = MagicMock()
    f = FillFollower(info, cb, state)
    f.follow(["0xaaa"])
    f.backfill()  # uses default
    payload = info.post.call_args.args[1]
    assert "startTime" in payload
    assert isinstance(payload["startTime"], int)


def test_callback_exception_does_not_propagate(state: State):
    info, captured = _info_capturing_subscribe()
    cb = MagicMock(side_effect=RuntimeError("boom"))
    f = FillFollower(info, cb, state)
    f.follow(["0xabc"])
    handler = captured["cbs"][0]  # type: ignore[index]
    # must not raise
    handler({"data": {"isSnapshot": False, "fills": [{"tid": 1}]}})
