"""Unit tests for the maker liveness watchdog decision logic."""
import importlib.util
from pathlib import Path

# Load scripts/maker_watchdog.py directly (scripts/ isn't a package).
_spec = importlib.util.spec_from_file_location(
    "maker_watchdog",
    Path(__file__).resolve().parent.parent / "scripts" / "maker_watchdog.py",
)
mw = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mw)


def test_healthy_single_process_fresh_log_no_alerts():
    assert mw.evaluate(pids=[123], log_age_s=10.0, stale_s=300.0) == []


def test_no_process_is_critical_down():
    alerts = mw.evaluate(pids=[], log_age_s=5.0, stale_s=300.0)
    assert len(alerts) == 1 and alerts[0][0] == "CRITICAL" and "DOWN" in alerts[0][1]


def test_multiple_processes_warn_duplicate():
    alerts = mw.evaluate(pids=[1, 2], log_age_s=5.0, stale_s=300.0)
    assert any(lvl == "WARN" and "duplicate" in msg for lvl, msg in alerts)


def test_stale_log_is_error():
    alerts = mw.evaluate(pids=[1], log_age_s=400.0, stale_s=300.0)
    assert any(lvl == "ERROR" and "stale" in msg for lvl, msg in alerts)


def test_down_and_stale_reports_both():
    alerts = mw.evaluate(pids=[], log_age_s=999.0, stale_s=300.0)
    levels = {lvl for lvl, _ in alerts}
    assert "CRITICAL" in levels and "ERROR" in levels


def test_missing_log_never_flags_stale():
    # log_age_s is None when the file doesn't exist yet — must not alert on staleness.
    assert mw.evaluate(pids=[1], log_age_s=None, stale_s=300.0) == []


def test_log_age_none_when_missing(tmp_path):
    assert mw.log_age_s(tmp_path / "nope.log", now=1000.0) is None


def test_log_age_computed_from_mtime(tmp_path):
    f = tmp_path / "maker.log"
    f.write_text("x")
    import os
    os.utime(f, (500.0, 500.0))
    assert mw.log_age_s(f, now=800.0) == 300.0
