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
    error_calls = [c for c in a.alert.call_args_list if c.args[0] == "error"]
    assert len(error_calls) == 1
    assert "stale" in error_calls[0].args[1].lower()


def test_recovery_alert_after_touch():
    a = MagicMock()
    h = ConnectionHealth(a, stale_threshold_s=0.1, check_interval_s=0.05)
    h._last_msg_ts = time.time() - 5  # type: ignore[attr-defined]
    h.start()
    time.sleep(0.2)
    h.touch()
    h.stop()
    levels = [c.args[0] for c in a.alert.call_args_list]
    assert "error" in levels and "info" in levels


def test_stop_idempotent():
    a = MagicMock()
    h = ConnectionHealth(a, stale_threshold_s=10)
    h.start()
    h.stop()
    h.stop()  # no-op
