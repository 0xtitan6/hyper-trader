from unittest.mock import MagicMock

from src.journal import Journal
from src.positions import PositionTracker
from src.state import State


def _make_tracker(state: State, journal: Journal) -> tuple[PositionTracker, list]:
    info = MagicMock()
    captured: list = []

    def subscribe(sub, cb):
        captured.append(cb)

    info.subscribe.side_effect = subscribe
    pt = PositionTracker(info, "0xacc", state, journal)
    pt.start()
    return pt, captured


def _msg(fills: list[dict], snapshot: bool = False) -> dict:
    return {"data": {"isSnapshot": snapshot, "fills": fills}}


def _f(
    tid: int,
    side: str,
    sz: float,
    px: float,
    closed_pnl: float = 0.0,
    fee: float = 0.0,
    coin: str = "#11",
    ts: int = 1714500000,
) -> dict:
    return {
        "tid": tid,
        "coin": coin,
        "side": side,
        "sz": str(sz),
        "px": str(px),
        "closedPnl": str(closed_pnl),
        "fee": str(fee),
        "time": ts * 1000,
    }


def test_start_subscribes_to_own_address(state, journal):
    info = MagicMock()
    pt = PositionTracker(info, "0xACC", state, journal)
    pt.start()
    info.subscribe.assert_called_once()
    sub = info.subscribe.call_args.args[0]
    assert sub == {"type": "userFills", "user": "0xacc"}


def test_start_idempotent(state, journal):
    info = MagicMock()
    pt = PositionTracker(info, "0xacc", state, journal)
    pt.start()
    pt.start()
    assert info.subscribe.call_count == 1


def test_buy_creates_long_position(state, journal):
    _pt, cbs = _make_tracker(state, journal)
    cbs[0](_msg([_f(1, "B", 100, 0.50)]))
    sz, avg = state.get_position("#11")
    assert sz == 100.0
    assert avg == 0.50


def test_two_buys_average_correctly(state, journal):
    _pt, cbs = _make_tracker(state, journal)
    cbs[0](_msg([_f(1, "B", 100, 0.50)]))
    cbs[0](_msg([_f(2, "B", 100, 0.70)]))
    sz, avg = state.get_position("#11")
    assert sz == 200.0
    assert avg == 0.60


def test_partial_sell_keeps_avg(state, journal):
    _pt, cbs = _make_tracker(state, journal)
    cbs[0](_msg([_f(1, "B", 100, 0.50)]))
    cbs[0](_msg([_f(2, "A", 30, 0.80)]))
    sz, avg = state.get_position("#11")
    assert sz == 70.0
    assert avg == 0.50  # avg unchanged on partial close


def test_full_sell_zeros_position(state, journal):
    _pt, cbs = _make_tracker(state, journal)
    cbs[0](_msg([_f(1, "B", 100, 0.50)]))
    cbs[0](_msg([_f(2, "A", 100, 0.80)]))
    sz, avg = state.get_position("#11")
    assert sz == 0.0
    assert avg == 0.0


def test_flip_long_to_short_resets_avg(state, journal):
    _pt, cbs = _make_tracker(state, journal)
    cbs[0](_msg([_f(1, "B", 100, 0.50)]))
    cbs[0](_msg([_f(2, "A", 150, 0.80)]))
    sz, avg = state.get_position("#11")
    assert sz == -50.0
    assert avg == 0.80


def test_realized_pnl_today_aggregates(state, journal):
    from src.positions import today_utc

    pt, cbs = _make_tracker(state, journal)
    today_ts = int(
        __import__("datetime").datetime.now(__import__("datetime").timezone.utc).timestamp()
    )
    cbs[0](
        _msg(
            [
                _f(1, "B", 10, 0.5, closed_pnl=2.5, ts=today_ts),
                _f(2, "A", 10, 0.7, closed_pnl=-1.5, ts=today_ts),
            ]
        )
    )
    assert pt.realized_pnl_today() == 1.0
    _ = today_utc()  # smoke


def test_duplicate_tid_not_reapplied(state, journal):
    _pt, cbs = _make_tracker(state, journal)
    cbs[0](_msg([_f(1, "B", 100, 0.50)]))
    cbs[0](_msg([_f(1, "B", 100, 0.50)]))  # same tid
    sz, _ = state.get_position("#11")
    assert sz == 100.0


def test_malformed_fill_skipped(state, journal):
    _pt, cbs = _make_tracker(state, journal)
    cbs[0](
        _msg(
            [
                {"tid": 1, "coin": "#11", "side": "?", "sz": "10", "px": "0.5"},  # bad side
                {"tid": 2, "coin": "", "side": "B", "sz": "10", "px": "0.5"},  # empty coin
                {"tid": 3, "coin": "#11", "side": "B", "sz": "0", "px": "0.5"},  # zero sz
                {"tid": 4, "coin": "#11", "side": "B", "sz": "10", "px": "0"},  # zero px
                {"tid": None, "coin": "#11", "side": "B", "sz": "10", "px": "0.5"},  # null tid
            ]
        )
    )
    assert state.get_position("#11") == (0.0, 0.0)


def test_total_exposure_sums_cost_basis(state, journal):
    pt, _ = _make_tracker(state, journal)
    state.update_position("#11", 100, 0.50)
    state.update_position("#12", -50, 0.70)
    assert pt.total_exposure_usd() == 100 * 0.50 + 50 * 0.70


def test_health_touched_on_msg(state, journal):
    info = MagicMock()
    captured: list = []
    info.subscribe.side_effect = lambda s, c: captured.append(c)
    health = MagicMock()
    pt = PositionTracker(info, "0xa", state, journal, health=health)
    pt.start()
    captured[0](_msg([]))
    health.touch.assert_called_once()


def test_reconcile_seeds_positions_from_user_state(state, journal):
    info = MagicMock()
    info.user_state.return_value = {
        "assetPositions": [
            {"position": {"coin": "#11", "szi": "100", "entryPx": "0.54"}},
            {"position": {"coin": "#12", "szi": "-50", "entryPx": "0.71"}},
        ]
    }
    pt = PositionTracker(info, "0xacc", state, journal)
    pt.reconcile_with_user_state()
    assert state.get_position("#11") == (100.0, 0.54)
    assert state.get_position("#12") == (-50.0, 0.71)


def test_reconcile_zeros_positions_missing_upstream(state, journal):
    """When a position exists locally but upstream user_state has dropped it
    (e.g. HIP-4 outcome settled), the local position must be zeroed."""
    state.update_position("#11", 100.0, 0.50)
    state.update_position("#12", 25.0, 0.40)
    info = MagicMock()
    info.user_state.return_value = {
        "assetPositions": [
            {"position": {"coin": "#11", "szi": "100", "entryPx": "0.50"}},
        ]
    }
    pt = PositionTracker(info, "0xacc", state, journal)
    pt.reconcile_with_user_state()
    assert state.get_position("#11") == (100.0, 0.50)
    assert state.get_position("#12") == (0.0, 0.0)  # settled away


def test_reconcile_overwrites_drift(state, journal):
    state.update_position("#11", 80.0, 0.45)  # local is stale
    info = MagicMock()
    info.user_state.return_value = {
        "assetPositions": [
            {"position": {"coin": "#11", "szi": "100", "entryPx": "0.50"}},
        ]
    }
    pt = PositionTracker(info, "0xacc", state, journal)
    pt.reconcile_with_user_state()
    assert state.get_position("#11") == (100.0, 0.50)


def test_reconcile_handles_user_state_failure(state, journal):
    """If user_state fails, keep local state untouched and don't raise."""
    state.update_position("#11", 100.0, 0.50)
    info = MagicMock()
    info.user_state.side_effect = RuntimeError("network")
    pt = PositionTracker(info, "0xacc", state, journal)
    result = pt.reconcile_with_user_state()
    assert state.get_position("#11") == (100.0, 0.50)
    assert result == {"#11": (100.0, 0.50)}


def test_reconcile_handles_malformed_position(state, journal):
    info = MagicMock()
    info.user_state.return_value = {
        "assetPositions": [
            {"position": {"coin": "#11", "szi": "garbage", "entryPx": "0.5"}},  # bad szi
            {"position": {"coin": "", "szi": "10", "entryPx": "0.5"}},  # empty coin
            {"position": {"coin": "#12", "szi": "20", "entryPx": "0.6"}},  # good
            {"position": "not a dict"},  # nested junk
            "not a dict",  # outer junk
        ]
    }
    pt = PositionTracker(info, "0xacc", state, journal)
    pt.reconcile_with_user_state()
    assert state.get_position("#12") == (20.0, 0.6)
    assert state.get_position("#11") == (0.0, 0.0)
    assert state.get_position("") == (0.0, 0.0)


def test_reconcile_picks_up_outcome_positions_from_spot(state, journal):
    """HIP-4 outcomes live in spotClearinghouseState.balances as `+NN`, NOT
    in assetPositions. Without this, local outcome positions get zeroed every
    reconcile cycle even though we still own them — this was a real bug
    that would have wiped our position state in production."""
    info = MagicMock()
    info.user_state.return_value = {"assetPositions": []}
    info.post.return_value = {
        "balances": [
            {"coin": "USDC", "total": "5.0", "entryNtl": "0.0"},
            {"coin": "+21", "total": "33.0", "entryNtl": "10.25"},  # our NO position
            {"coin": "+20", "total": "0.0", "entryNtl": "0.0"},  # zeroed leg
        ]
    }
    pt = PositionTracker(info, "0xacc", state, journal)
    result = pt.reconcile_with_user_state()
    # Translated +21 -> #21 with avg_px = 10.25/33
    assert "#21" in result
    sz, avg = result["#21"]
    assert sz == 33.0
    assert abs(avg - 10.25 / 33) < 1e-6


def test_reconcile_handles_spot_fetch_failure(state, journal):
    """If spotClearinghouseState fails, perp reconcile must still work."""
    info = MagicMock()
    info.user_state.return_value = {
        "assetPositions": [{"position": {"coin": "BTC", "szi": "0.1", "entryPx": "50000"}}]
    }
    info.post.side_effect = RuntimeError("spot endpoint down")
    pt = PositionTracker(info, "0xacc", state, journal)
    result = pt.reconcile_with_user_state()
    assert result == {"BTC": (0.1, 50000.0)}


def test_reconcile_skips_non_outcome_spot_balances(state, journal):
    """Only `+NN` outcome legs become positions; USDC/USDH/etc are not."""
    info = MagicMock()
    info.user_state.return_value = {"assetPositions": []}
    info.post.return_value = {
        "balances": [
            {"coin": "USDC", "total": "100.0", "entryNtl": "0.0"},
            {"coin": "USDH", "total": "50.0", "entryNtl": "50.0"},
            {"coin": "PURR", "total": "10.0", "entryNtl": "10.0"},  # spot token, not outcome
        ]
    }
    pt = PositionTracker(info, "0xacc", state, journal)
    result = pt.reconcile_with_user_state()
    assert result == {}


def test_settlement_fill_handled_specially(state, journal):
    """A settlement fill (dir=Settlement, px=0) must zero the position and
    record the closed_pnl, not be rejected as malformed for px<=0."""
    info = MagicMock()
    captured: list = []
    info.subscribe.side_effect = lambda s, c: captured.append(c)
    state.update_position("#21", 33.0, 0.31062)  # we own 33 NO shares
    pt = PositionTracker(info, "0xacc", state, journal)
    pt.start()

    settlement_fill = {
        "tid": 999,
        "coin": "#21",
        "side": "A",
        "sz": "33.0",
        "px": "0.0",
        "closedPnl": "-10.25",
        "fee": "0.0",
        "time": 1714500000000,
        "dir": "Settlement",
    }
    captured[0]({"data": {"isSnapshot": False, "fills": [settlement_fill]}})

    # Position should be zeroed
    sz, _avg = state.get_position("#21")
    assert sz == 0.0
    # Daily P&L should reflect the settlement loss
    pnl, _fee = state.daily_pnl(
        __import__("datetime")
        .datetime.fromtimestamp(1714500000, tz=__import__("datetime").timezone.utc)
        .strftime("%Y-%m-%d")
    )
    assert abs(pnl - (-10.25)) < 1e-6


def test_settlement_emits_critical_alert(state, journal):
    """Settlement must alert critically so webhooks fire even when no agent
    is supervising — this was the failure mode that lost the operator's
    settlement notification on the BTC binary."""
    info = MagicMock()
    captured: list = []
    info.subscribe.side_effect = lambda s, c: captured.append(c)
    alerter = MagicMock()
    pt = PositionTracker(info, "0xacc", state, journal, alerter=alerter)
    pt.start()

    settlement_fill = {
        "tid": 1000,
        "coin": "#21",
        "side": "A",
        "sz": "33.0",
        "px": "0.0",
        "closedPnl": "-10.25",
        "fee": "0.0",
        "time": 1714500000000,
        "dir": "Settlement",
    }
    captured[0]({"data": {"isSnapshot": False, "fills": [settlement_fill]}})

    # Critical alert with verdict
    crit = [c for c in alerter.alert.call_args_list if c.args[0] == "critical"]
    assert len(crit) == 1
    assert "SETTLEMENT" in crit[0].args[1]
    assert "lost" in crit[0].args[1]


def test_settlement_winning_position_emits_won(state, journal):
    info = MagicMock()
    captured: list = []
    info.subscribe.side_effect = lambda s, c: captured.append(c)
    alerter = MagicMock()
    pt = PositionTracker(info, "0xacc", state, journal, alerter=alerter)
    pt.start()
    win_fill = {
        "tid": 1001,
        "coin": "#20",
        "side": "A",
        "sz": "10.0",
        "px": "1.0",
        "closedPnl": "5.0",
        "fee": "0.0",
        "time": 1714500000000,
        "dir": "Settlement",
    }
    captured[0]({"data": {"isSnapshot": False, "fills": [win_fill]}})
    crit = [c for c in alerter.alert.call_args_list if c.args[0] == "critical"]
    assert "won" in crit[0].args[1]


def test_spot_pair_fill_does_not_create_position(state, journal):
    """HL spot pair fills (coin starts with `@`) are stablecoin swaps and other
    spot trades — they must NOT be tracked as positions. Real-world bug: a
    USDC→USDH swap on @230 was registered as a phantom short, eating $54 of
    our $60 exposure cap and blocking all perp mirroring."""
    _pt, cbs = _make_tracker(state, journal)
    cbs[0](_msg([_f(1, "A", 54.73, 1.0, coin="@230")]))
    sz, _avg = state.get_position("@230")
    assert sz == 0.0, "spot pair must not become a tracked position"


def test_spot_pair_fill_with_slash_skipped(state, journal):
    """Slash notation (e.g. PURR/USDC) is also a spot pair — same skip rule."""
    _pt, cbs = _make_tracker(state, journal)
    cbs[0](_msg([_f(2, "B", 100.0, 1.5, coin="PURR/USDC")]))
    sz, _ = state.get_position("PURR/USDC")
    assert sz == 0.0


def test_spot_pair_fill_journals_skip_event(state, journal):
    """Skipped fills get a forensic record so we can audit what was filtered."""
    _pt, cbs = _make_tracker(state, journal)
    cbs[0](_msg([_f(3, "A", 10.0, 1.0, coin="@230")]))
    import json

    with open(journal.path) as f:
        events = [json.loads(line)["event"] for line in f]
    assert "own_fill_skipped" in events


def test_perp_and_outcome_fills_still_tracked(state, journal):
    """Filter must NOT affect perps (BTC, ETH, etc.) or outcomes (#NN)."""
    _pt, cbs = _make_tracker(state, journal)
    cbs[0](
        _msg(
            [
                _f(10, "B", 0.001, 50000.0, coin="BTC"),  # perp — should track
                _f(11, "B", 100.0, 0.50, coin="#11"),  # outcome — should track
            ]
        )
    )
    btc_sz, _ = state.get_position("BTC")
    out_sz, _ = state.get_position("#11")
    assert btc_sz == 0.001
    assert out_sz == 100.0


def test_settlement_journals_event(state, journal, tmp_path):
    info = MagicMock()
    captured: list = []
    info.subscribe.side_effect = lambda s, c: captured.append(c)
    pt = PositionTracker(info, "0xacc", state, journal)
    pt.start()
    fill = {
        "tid": 2000,
        "coin": "#21",
        "side": "A",
        "sz": "33.0",
        "px": "0.0",
        "closedPnl": "-10.25",
        "fee": "0.0",
        "time": 1714500000000,
        "dir": "Settlement",
    }
    captured[0]({"data": {"isSnapshot": False, "fills": [fill]}})
    import json

    with open(journal.path) as f:
        events = [json.loads(line) for line in f]
    settlements = [e for e in events if e["event"] == "settlement"]
    assert len(settlements) == 1
    assert settlements[0]["coin"] == "#21"
    assert settlements[0]["verdict"] == "lost"
    assert settlements[0]["closed_pnl"] == -10.25


def test_reconcile_journals_event(state, journal, tmp_path):
    info = MagicMock()
    info.user_state.return_value = {
        "assetPositions": [
            {"position": {"coin": "#11", "szi": "10", "entryPx": "0.5"}},
        ]
    }
    state.update_position("#stale", 5.0, 0.3)
    pt = PositionTracker(info, "0xacc", state, journal)
    pt.reconcile_with_user_state()
    import json

    with open(journal.path) as f:
        lines = [json.loads(line) for line in f]
    reconcile_events = [e for e in lines if e["event"] == "reconcile"]
    assert len(reconcile_events) == 1
    assert "#stale" in reconcile_events[0]["zeroed"]
