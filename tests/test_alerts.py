from unittest.mock import MagicMock

import pytest

from src.alerts import NullAlerter, WebhookAlerter


def test_null_alerter_no_op():
    NullAlerter().alert("info", "hi")  # must not raise


def test_webhook_invalid_min_level():
    with pytest.raises(ValueError):
        WebhookAlerter("https://example.com/hook", min_level="panic")


def test_webhook_calls_post_above_threshold():
    post = MagicMock()
    a = WebhookAlerter("https://example.com/hook", min_level="warn", post=post)
    a.alert("error", "broken")
    post.assert_called_once()
    args, kwargs = post.call_args
    assert args[0] == "https://example.com/hook"
    assert "ERROR" in kwargs["json"]["text"]
    assert "broken" in kwargs["json"]["text"]


def test_webhook_skips_below_threshold():
    post = MagicMock()
    a = WebhookAlerter("https://example.com/hook", min_level="warn", post=post)
    a.alert("info", "noise")
    post.assert_not_called()


def test_webhook_disabled_when_url_empty():
    post = MagicMock()
    a = WebhookAlerter("", min_level="info", post=post)
    a.alert("error", "broken")
    post.assert_not_called()


def test_webhook_post_failure_swallowed():
    post = MagicMock(side_effect=RuntimeError("network down"))
    a = WebhookAlerter("https://example.com/hook", min_level="info", post=post)
    a.alert("error", "broken")  # must not raise


def test_unknown_level_treated_as_info():
    post = MagicMock()
    a = WebhookAlerter("https://example.com/hook", min_level="info", post=post)
    a.alert("nonsense", "x")  # downgraded to info
    post.assert_called_once()
