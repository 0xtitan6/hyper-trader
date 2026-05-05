import threading
import time
from unittest.mock import MagicMock

import pytest

from src.alerts import NullAlerter, WebhookAlerter


def test_null_alerter_no_op():
    a = NullAlerter()
    a.alert("info", "hi")  # must not raise
    a.stop()  # must not raise


def test_webhook_invalid_min_level():
    with pytest.raises(ValueError):
        WebhookAlerter("https://example.com/hook", min_level="panic", synchronous=True)


def test_webhook_calls_post_above_threshold():
    post = MagicMock()
    a = WebhookAlerter("https://example.com/hook", min_level="warn", post=post, synchronous=True)
    a.alert("error", "broken")
    post.assert_called_once()
    args, kwargs = post.call_args
    assert args[0] == "https://example.com/hook"
    assert "ERROR" in kwargs["json"]["text"]
    assert "broken" in kwargs["json"]["text"]


def test_webhook_skips_below_threshold():
    post = MagicMock()
    a = WebhookAlerter("https://example.com/hook", min_level="warn", post=post, synchronous=True)
    a.alert("info", "noise")
    post.assert_not_called()


def test_webhook_disabled_when_url_empty():
    post = MagicMock()
    a = WebhookAlerter("", min_level="info", post=post, synchronous=True)
    a.alert("error", "broken")
    post.assert_not_called()


def test_webhook_post_failure_swallowed():
    post = MagicMock(side_effect=RuntimeError("network down"))
    a = WebhookAlerter("https://example.com/hook", min_level="info", post=post, synchronous=True)
    a.alert("error", "broken")  # must not raise


def test_unknown_level_treated_as_info():
    post = MagicMock()
    a = WebhookAlerter("https://example.com/hook", min_level="info", post=post, synchronous=True)
    a.alert("nonsense", "x")  # downgraded to info
    post.assert_called_once()


def test_async_delivery_via_worker():
    """In async mode, alerts are dispatched via a daemon worker thread."""
    delivered = threading.Event()

    def post(*_a, **_kw):
        delivered.set()

    a = WebhookAlerter("https://example.com/hook", min_level="info", post=post)
    try:
        a.alert("error", "boom")
        assert delivered.wait(timeout=2.0), "worker did not deliver alert"
    finally:
        a.stop()


def test_async_does_not_block_caller_on_slow_webhook():
    """A slow webhook host must not block the alert() caller."""

    def slow_post(*_a, **_kw):
        time.sleep(2.0)

    a = WebhookAlerter("https://example.com/hook", min_level="info", post=slow_post)
    try:
        t0 = time.perf_counter()
        a.alert("error", "boom")
        elapsed = time.perf_counter() - t0
        # Must return well before 2s of webhook latency
        assert elapsed < 0.5, f"alert() blocked for {elapsed:.2f}s"
    finally:
        a.stop()


def test_async_drops_when_queue_full(caplog):
    """When the bounded queue saturates, drops are warned and don't block."""
    # Make every post hang forever so the worker can't drain
    started = threading.Event()
    block = threading.Event()

    def hang_post(*_a, **_kw):
        started.set()
        block.wait()

    a = WebhookAlerter("https://example.com/hook", min_level="info", post=hang_post)
    try:
        # Fill the queue: one is in-flight (blocked), the rest pile up
        for i in range(300):
            a.alert("error", f"msg-{i}")
        # Saturated — at least one drop should have been warned
        assert any("queue full" in rec.getMessage().lower() for rec in caplog.records), (
            "expected a 'queue full' warning"
        )
    finally:
        block.set()
        a.stop()


def test_telegram_url_uses_chat_id_format(monkeypatch):
    """Telegram bot API requires chat_id + text (not Slack-style {text:...})."""
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "6576043283")
    post = MagicMock()
    url = "https://api.telegram.org/bot12345:abc/sendMessage"
    a = WebhookAlerter(url, min_level="info", post=post, synchronous=True)
    a.alert("critical", "settlement -10.25")
    post.assert_called_once()
    args, kwargs = post.call_args
    assert args[0] == url
    body = kwargs["json"]
    assert body["chat_id"] == "6576043283"
    assert "CRITICAL" in body["text"]
    assert "settlement -10.25" in body["text"]
    assert "text" in body and len(body) == 2  # Telegram needs both fields


def test_telegram_url_drops_alert_when_chat_id_missing(monkeypatch, caplog):
    """If TELEGRAM_CHAT_ID env is unset, a Telegram URL can't deliver — drop with warning."""
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    post = MagicMock()
    url = "https://api.telegram.org/bot12345:abc/sendMessage"
    a = WebhookAlerter(url, min_level="info", post=post, synchronous=True)
    a.alert("error", "broken")
    post.assert_not_called()
    assert any("TELEGRAM_CHAT_ID" in rec.getMessage() for rec in caplog.records), (
        "expected a warning naming TELEGRAM_CHAT_ID"
    )


def test_non_telegram_url_uses_slack_format(monkeypatch):
    """Slack/Discord/generic webhooks get the historical {text: ...} body."""
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "12345")  # should be ignored for non-Telegram URLs
    post = MagicMock()
    a = WebhookAlerter(
        "https://hooks.slack.example/abc", min_level="info", post=post, synchronous=True
    )
    a.alert("warn", "stale ws")
    post.assert_called_once()
    body = post.call_args.kwargs["json"]
    assert "chat_id" not in body
    assert body["text"] == "[WARN] stale ws"


def test_stop_idempotent_on_null_alerter():
    a = NullAlerter()
    a.stop()
    a.stop()  # no-op


def test_stop_idempotent_on_webhook():
    post = MagicMock()
    a = WebhookAlerter("https://example.com/hook", min_level="info", post=post)
    a.stop()
    a.stop()  # no-op
