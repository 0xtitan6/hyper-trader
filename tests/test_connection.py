import time
from unittest.mock import MagicMock

import pytest

from src.connection import ConnectionHealth


def test_invalid_threshold_raises():
    with pytest.raises(ValueError):
        ConnectionHealth(MagicMock(), stale_threshold_s=0)


def test_touch_resets_age():
    a = MagicMock()
    h = ConnectionHealth(a, stale_threshold_s=10)
    h._last_msg_ts = time.time() - 50  # type: ignore[attr-defined]
    assert h.last_msg_age_s() > 40
    h.touch()
    assert h.last_msg_age_s() < 1


def test_stale_alert_fires_once():
    a = MagicMock()
    h = ConnectionHealth(a, stale_threshold_s=0.1, check_interval_s=0.05)
    h._last_msg_ts = time.time() - 5  # type: ignore[attr-defined]
    h.start()
    time.sleep(0.3)
    h.stop()
    # WS stale fires "warn" (not "error") after 2026-05-10 demote — these
    # events are routine HL connection cycles, not real failures.
    warn_calls = [c for c in a.alert.call_args_list if c.args[0] == "warn"]
    assert len(warn_calls) == 1
    assert "stale" in warn_calls[0].args[1].lower()


def test_recovery_alert_after_touch():
    a = MagicMock()
    h = ConnectionHealth(a, stale_threshold_s=0.1, check_interval_s=0.05)
    h._last_msg_ts = time.time() - 5  # type: ignore[attr-defined]
    h.start()
    time.sleep(0.2)
    h.touch()
    h.stop()
    levels = [c.args[0] for c in a.alert.call_args_list]
    assert "warn" in levels and "info" in levels  # stale=warn, recovery=info


def test_stop_idempotent():
    a = MagicMock()
    h = ConnectionHealth(a, stale_threshold_s=10)
    h.start()
    h.stop()
    h.stop()  # no-op
