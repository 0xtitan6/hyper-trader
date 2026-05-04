"""Hypothesis property tests for position math.

We create fresh State/Journal per generated example (hypothesis can't safely
share function-scoped fixtures across examples).

Invariants:
1. Buy then equal-size sell zeros the position regardless of prices.
2. Two same-side fills produce a weighted-avg price between the two fill prices.
3. Position size always equals the sum of signed deltas applied.
4. Average price never goes negative.
5. Replaying the same tid is idempotent.
"""

from pathlib import Path
from unittest.mock import MagicMock

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from src.journal import Journal
from src.positions import PositionTracker
from src.state import State

_HYP_SETTINGS = settings(
    max_examples=40,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)


def _make_tracker(tmp_path: Path) -> tuple[PositionTracker, list, State]:
    state = State(str(tmp_path / "state.db"))
    journal = Journal(str(tmp_path / "j.jsonl"))
    info = MagicMock()
    captured: list = []
    info.subscribe.side_effect = lambda s, c: captured.append(c)
    pt = PositionTracker(info, "0xacc", state, journal)
    pt.start()
    return pt, captured, state


def _fill(tid, side, sz, px, ts=1714000000):
    return {
        "tid": tid,
        "coin": "#11",
        "side": side,
        "sz": str(sz),
        "px": str(px),
        "fee": "0",
        "closedPnl": "0",
        "time": ts * 1000,
    }


@given(
    px1=st.floats(min_value=0.01, max_value=1.0, allow_nan=False, allow_infinity=False),
    sz1=st.integers(min_value=1, max_value=10000),
    px2=st.floats(min_value=0.01, max_value=1.0, allow_nan=False, allow_infinity=False),
)
@_HYP_SETTINGS
def test_full_close_zeros_position(tmp_path_factory, px1, sz1, px2):
    tmp = tmp_path_factory.mktemp("p")
    _pt, cbs, state = _make_tracker(tmp)
    cb = cbs[0]
    cb({"data": {"isSnapshot": False, "fills": [_fill(1, "B", sz1, px1)]}})
    cb({"data": {"isSnapshot": False, "fills": [_fill(2, "A", sz1, px2)]}})
    sz, _avg = state.get_position("#11")
    assert sz == 0.0


@given(
    px1=st.floats(min_value=0.01, max_value=1.0, allow_nan=False),
    sz1=st.integers(min_value=1, max_value=10000),
    px2=st.floats(min_value=0.01, max_value=1.0, allow_nan=False),
    sz2=st.integers(min_value=1, max_value=10000),
)
@_HYP_SETTINGS
def test_avg_price_between_for_same_direction_add(tmp_path_factory, px1, sz1, px2, sz2):
    tmp = tmp_path_factory.mktemp("p")
    _pt, cbs, state = _make_tracker(tmp)
    cb = cbs[0]
    cb({"data": {"isSnapshot": False, "fills": [_fill(1, "B", sz1, px1)]}})
    cb({"data": {"isSnapshot": False, "fills": [_fill(2, "B", sz2, px2)]}})
    _sz, avg = state.get_position("#11")
    assert min(px1, px2) - 1e-9 <= avg <= max(px1, px2) + 1e-9


@given(
    fills=st.lists(
        st.tuples(
            st.sampled_from(["B", "A"]),
            st.integers(min_value=1, max_value=100),
            st.floats(min_value=0.01, max_value=1.0, allow_nan=False),
        ),
        min_size=1,
        max_size=20,
    )
)
@_HYP_SETTINGS
def test_signed_size_equals_sum_of_deltas(tmp_path_factory, fills):
    tmp = tmp_path_factory.mktemp("p")
    _pt, cbs, state = _make_tracker(tmp)
    cb = cbs[0]
    expected_sz = 0
    for i, (side, sz, px) in enumerate(fills):
        cb({"data": {"isSnapshot": False, "fills": [_fill(i + 1, side, sz, px)]}})
        expected_sz += sz if side == "B" else -sz
    actual_sz, _ = state.get_position("#11")
    assert abs(actual_sz - expected_sz) < 1e-9


@given(
    px=st.floats(min_value=0.01, max_value=1.0, allow_nan=False),
    sz=st.integers(min_value=1, max_value=10000),
    n_dups=st.integers(min_value=1, max_value=5),
)
@_HYP_SETTINGS
def test_duplicate_tids_idempotent(tmp_path_factory, px, sz, n_dups):
    tmp = tmp_path_factory.mktemp("p")
    _pt, cbs, state = _make_tracker(tmp)
    cb = cbs[0]
    cb({"data": {"isSnapshot": False, "fills": [_fill(42, "B", sz, px)]}})
    sz1, avg1 = state.get_position("#11")
    for _ in range(n_dups):
        cb({"data": {"isSnapshot": False, "fills": [_fill(42, "B", sz, px)]}})
    sz2, avg2 = state.get_position("#11")
    assert sz1 == sz2
    assert avg1 == avg2


@given(
    fills=st.lists(
        st.tuples(
            st.sampled_from(["B", "A"]),
            st.integers(min_value=1, max_value=100),
            st.floats(min_value=0.01, max_value=1.0, allow_nan=False),
        ),
        min_size=1,
        max_size=10,
    )
)
@_HYP_SETTINGS
def test_avg_price_never_negative(tmp_path_factory, fills):
    tmp = tmp_path_factory.mktemp("p")
    _pt, cbs, state = _make_tracker(tmp)
    cb = cbs[0]
    for i, (side, sz, px) in enumerate(fills):
        cb({"data": {"isSnapshot": False, "fills": [_fill(i + 1, side, sz, px)]}})
    _sz, avg = state.get_position("#11")
    assert avg >= 0
