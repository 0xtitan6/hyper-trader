import io
import json
import logging
import sys

from src.log import JsonFormatter, setup_logging


def _capture(level: str, json_mode: bool) -> tuple[io.StringIO, logging.Logger]:
    buf = io.StringIO()
    setup_logging(level=level, json_mode=json_mode)
    # Replace handler stream with our buffer
    root = logging.getLogger()
    for h in root.handlers:
        h.stream = buf  # type: ignore[attr-defined]
    return buf, logging.getLogger("test")


def test_setup_plain_format(capsys):
    setup_logging(level="INFO", json_mode=False)
    logging.getLogger("test").info("hello world")
    captured = capsys.readouterr()
    out = captured.out + captured.err
    # Plain format includes "INFO" and the message but not raw JSON
    assert "INFO" in out
    assert "hello world" in out
    assert not out.strip().startswith("{")


def test_setup_json_format():
    setup_logging(level="INFO", json_mode=True)
    buf = io.StringIO()
    root = logging.getLogger()
    for h in root.handlers:
        h.stream = buf  # type: ignore[attr-defined]
    logging.getLogger("test").info("hi")
    out = buf.getvalue().strip()
    assert out
    payload = json.loads(out)
    assert payload["msg"] == "hi"
    assert payload["level"] == "INFO"
    assert payload["logger"] == "test"
    assert "ts" in payload


def test_json_formatter_includes_exception():
    f = JsonFormatter()
    try:
        raise ValueError("boom")
    except ValueError:
        record = logging.LogRecord(
            "x",
            logging.ERROR,
            "p",
            1,
            "msg",
            (),
            sys.exc_info(),
        )
    out = json.loads(f.format(record))
    assert out["level"] == "ERROR"
    assert "boom" in out["exc"]


def test_setup_idempotent_replaces_handlers():
    setup_logging(level="INFO", json_mode=False)
    setup_logging(level="DEBUG", json_mode=True)
    root = logging.getLogger()
    # Only one handler should remain after re-setup
    assert len(root.handlers) == 1
    assert root.level == logging.DEBUG
