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
