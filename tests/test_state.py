from pathlib import Path

from src.state import State


def test_init_creates_db_and_dir(tmp_path: Path):
    db_path = tmp_path / "nested" / "state.db"
    State(str(db_path))
    assert db_path.exists()


def test_mark_tid_seen_first_returns_true(state: State):
    assert state.mark_tid_seen(1, "0xabc") is True


def test_mark_tid_seen_duplicate_returns_false(state: State):
    state.mark_tid_seen(1, "0xabc")
    assert state.mark_tid_seen(1, "0xabc") is False


def test_is_tid_seen(state: State):
    assert state.is_tid_seen(99) is False
    state.mark_tid_seen(99, "0xabc")
    assert state.is_tid_seen(99) is True


def test_record_own_fill_first_returns_true(state: State):
    assert (
        state.record_own_fill(
            tid=1,
            coin="#11",
            side="B",
            sz=10.0,
            px=0.5,
            closed_pnl=0.0,
            fee=0.01,
            ts=1714000000,
            date_utc="2026-05-03",
        )
        is True
    )


def test_record_own_fill_duplicate_returns_false(state: State):
    state.record_own_fill(1, "#11", "B", 10, 0.5, 0, 0.01, 1714000000, "2026-05-03")
    assert state.record_own_fill(1, "#11", "B", 10, 0.5, 0, 0.01, 1714000000, "2026-05-03") is False


def test_daily_pnl_sums_across_fills(state: State):
    state.record_own_fill(1, "#11", "B", 10, 0.5, 0.0, 0.01, 0, "2026-05-03")
    state.record_own_fill(2, "#11", "A", 10, 0.7, 2.0, 0.02, 0, "2026-05-03")
    state.record_own_fill(3, "#11", "B", 5, 0.6, -0.5, 0.01, 0, "2026-05-03")
    pnl, fee = state.daily_pnl("2026-05-03")
    assert pnl == 1.5
    assert abs(fee - 0.04) < 1e-9


def test_daily_pnl_other_dates_excluded(state: State):
    state.record_own_fill(1, "#11", "B", 10, 0.5, 5.0, 0.01, 0, "2026-05-03")
    state.record_own_fill(2, "#11", "B", 10, 0.5, 3.0, 0.01, 0, "2026-05-04")
    pnl, _ = state.daily_pnl("2026-05-03")
    assert pnl == 5.0


def test_daily_pnl_empty_date(state: State):
    pnl, fee = state.daily_pnl("2099-01-01")
    assert pnl == 0.0
    assert fee == 0.0


def test_get_position_default_zero(state: State):
    assert state.get_position("#11") == (0.0, 0.0)


def test_update_and_get_position(state: State):
    state.update_position("#11", 100.0, 0.55)
    sz, avg = state.get_position("#11")
    assert sz == 100.0
    assert avg == 0.55


def test_update_position_overwrites(state: State):
    state.update_position("#11", 100.0, 0.55)
    state.update_position("#11", 50.0, 0.60)
    assert state.get_position("#11") == (50.0, 0.60)


def test_get_positions_returns_all(state: State):
    state.update_position("#11", 10, 0.5)
    state.update_position("#12", -5, 0.7)
    positions = state.get_positions()
    assert positions == {"#11": (10.0, 0.5), "#12": (-5.0, 0.7)}


def test_schema_idempotent(tmp_path: Path):
    db = str(tmp_path / "s.db")
    s1 = State(db)
    s1.mark_tid_seen(1, "0xa")
    s2 = State(db)  # re-init same path
    assert s2.is_tid_seen(1) is True


def test_unmark_tid_seen_allows_retry(state: State):
    state.mark_tid_seen(42, "0xabc")
    assert state.is_tid_seen(42) is True
    assert state.unmark_tid_seen(42) is True
    assert state.is_tid_seen(42) is False
    # After unmark, mark_tid_seen succeeds again (i.e. fill can re-dispatch)
    assert state.mark_tid_seen(42, "0xabc") is True


def test_unmark_tid_seen_missing_returns_false(state: State):
    assert state.unmark_tid_seen(9999) is False


def test_concurrent_mark_tid_seen_only_one_wins(tmp_path: Path):
    import threading

    s = State(str(tmp_path / "s.db"))
    results: list[bool] = []
    lock = threading.Lock()

    def worker():
        ok = s.mark_tid_seen(42, "0xa")
        with lock:
            results.append(ok)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sum(results) == 1
    assert sum(1 for r in results if not r) == 7
