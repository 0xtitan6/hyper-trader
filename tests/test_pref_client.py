"""Tests for src/pref_client.py — minimal PREF MCP HTTP client."""

from __future__ import annotations

import json
import urllib.error
from pathlib import Path
from unittest.mock import patch

import pytest

from src.pref_client import PrefClient


@pytest.fixture
def credentials_file(tmp_path: Path) -> Path:
    f = tmp_path / "credentials.json"
    f.write_text(json.dumps({"api_key": "pref_agent_test_key_xyz"}))
    f.chmod(0o600)
    return f


def _ok_response(payload: dict) -> bytes:
    """Build a JSON-RPC success envelope wrapping `payload` as the inner JSON."""
    envelope = {
        "jsonrpc": "2.0",
        "id": 1,
        "result": {"content": [{"type": "text", "text": json.dumps(payload)}]},
    }
    return json.dumps(envelope).encode()


def _structured_response(payload: dict) -> bytes:
    envelope = {
        "jsonrpc": "2.0",
        "id": 1,
        "result": {"structuredContent": payload},
    }
    return json.dumps(envelope).encode()


def _mock_urlopen(body: bytes):
    """Return a context-manager mock that yields a body when entered."""
    class _MockResp:
        def __enter__(self_):
            return self_

        def __exit__(self_, *a):
            return False

        def read(self_):
            return body

    return _MockResp()


# --- key loading ---------------------------------------------------------


def test_load_key_caches_after_first_read(credentials_file: Path):
    c = PrefClient(credentials_path=credentials_file)
    assert c._load_key() == "pref_agent_test_key_xyz"
    # Manually invalidate file to prove cache hit
    credentials_file.write_text("garbage")
    assert c._load_key() == "pref_agent_test_key_xyz"  # cached


def test_load_key_returns_none_when_file_missing(tmp_path: Path):
    c = PrefClient(credentials_path=tmp_path / "does-not-exist.json")
    assert c._load_key() is None


def test_load_key_returns_none_when_file_invalid_json(tmp_path: Path):
    f = tmp_path / "creds.json"
    f.write_text("{ not valid json")
    c = PrefClient(credentials_path=f)
    assert c._load_key() is None


def test_load_key_returns_none_when_no_api_key_field(tmp_path: Path):
    f = tmp_path / "creds.json"
    f.write_text(json.dumps({"other_field": "value"}))
    c = PrefClient(credentials_path=f)
    assert c._load_key() is None


# --- call_tool happy path -----------------------------------------------


def test_call_tool_success_returns_parsed_payload(credentials_file: Path):
    c = PrefClient(credentials_path=credentials_file)
    payload = {"value": 42, "classification": "Fear"}
    with patch("urllib.request.urlopen", return_value=_mock_urlopen(_ok_response(payload))):
        result = c.call_tool("crypto.get_fear_greed_index")
    assert result == payload


def test_call_tool_prefers_structured_content_when_present(credentials_file: Path):
    """Some tools return data in result.structuredContent — use that path
    when available, falling back to content[0].text otherwise."""
    c = PrefClient(credentials_path=credentials_file)
    payload = {"value": 50, "classification": "Neutral"}
    with patch("urllib.request.urlopen", return_value=_mock_urlopen(_structured_response(payload))):
        result = c.call_tool("crypto.get_fear_greed_index")
    assert result == payload


def test_call_tool_sends_authorization_bearer(credentials_file: Path):
    c = PrefClient(credentials_path=credentials_file)
    captured = {}

    def _capture(req, **kwargs):
        captured["headers"] = dict(req.header_items())
        captured["body"] = req.data
        return _mock_urlopen(_ok_response({}))

    with patch("urllib.request.urlopen", side_effect=_capture):
        c.call_tool("noop")

    # Header key casing varies by urllib version; normalize
    auth = next((v for k, v in captured["headers"].items() if k.lower() == "authorization"), None)
    assert auth == "Bearer pref_agent_test_key_xyz"
    accept = next((v for k, v in captured["headers"].items() if k.lower() == "accept"), None)
    assert "application/json" in (accept or "")


def test_call_tool_passes_arguments(credentials_file: Path):
    c = PrefClient(credentials_path=credentials_file)
    captured = {}

    def _capture(req, **kwargs):
        captured["body"] = json.loads(req.data.decode())
        return _mock_urlopen(_ok_response({}))

    with patch("urllib.request.urlopen", side_effect=_capture):
        c.call_tool("polymarket.discovery.search_unified", {"query": "election"})

    assert captured["body"]["params"]["name"] == "polymarket.discovery.search_unified"
    assert captured["body"]["params"]["arguments"] == {"query": "election"}


# --- failure modes -------------------------------------------------------


def test_call_tool_returns_none_without_credentials(tmp_path: Path):
    c = PrefClient(credentials_path=tmp_path / "missing.json")
    # Should NOT make HTTP call when key missing
    with patch("urllib.request.urlopen") as mock_open:
        result = c.call_tool("noop")
    assert result is None
    mock_open.assert_not_called()


def test_call_tool_returns_none_on_http_error(credentials_file: Path):
    c = PrefClient(credentials_path=credentials_file)
    with patch(
        "urllib.request.urlopen",
        side_effect=urllib.error.HTTPError(
            "url", 502, "bad gateway", {}, None  # type: ignore[arg-type]
        ),
    ):
        result = c.call_tool("noop")
    assert result is None


def test_call_tool_returns_none_on_timeout(credentials_file: Path):
    c = PrefClient(credentials_path=credentials_file)
    with patch("urllib.request.urlopen", side_effect=TimeoutError("slow")):
        result = c.call_tool("noop")
    assert result is None


def test_call_tool_returns_none_on_non_json_response(credentials_file: Path):
    c = PrefClient(credentials_path=credentials_file)
    with patch("urllib.request.urlopen", return_value=_mock_urlopen(b"not json")):
        result = c.call_tool("noop")
    assert result is None


def test_call_tool_returns_none_on_jsonrpc_error(credentials_file: Path):
    c = PrefClient(credentials_path=credentials_file)
    envelope = json.dumps({"jsonrpc": "2.0", "id": 1,
                           "error": {"code": -32601, "message": "Method not found"}}).encode()
    with patch("urllib.request.urlopen", return_value=_mock_urlopen(envelope)):
        result = c.call_tool("noop")
    assert result is None


def test_call_tool_returns_none_on_missing_content(credentials_file: Path):
    c = PrefClient(credentials_path=credentials_file)
    envelope = json.dumps({"jsonrpc": "2.0", "id": 1, "result": {}}).encode()
    with patch("urllib.request.urlopen", return_value=_mock_urlopen(envelope)):
        result = c.call_tool("noop")
    assert result is None


def test_call_tool_returns_none_when_inner_text_not_json(credentials_file: Path):
    c = PrefClient(credentials_path=credentials_file)
    envelope = json.dumps({
        "jsonrpc": "2.0", "id": 1,
        "result": {"content": [{"type": "text", "text": "this is not json"}]}
    }).encode()
    with patch("urllib.request.urlopen", return_value=_mock_urlopen(envelope)):
        result = c.call_tool("noop")
    assert result is None


# --- get_fear_greed_index convenience wrapper ----------------------------


def test_get_fear_greed_index_returns_tuple(credentials_file: Path):
    c = PrefClient(credentials_path=credentials_file)
    payload = {"value": 22, "classification": "Extreme Fear",
               "timestamp": "2026-05-27T10:00:00Z", "time_until_update": "3600"}
    with patch("urllib.request.urlopen", return_value=_mock_urlopen(_ok_response(payload))):
        result = c.get_fear_greed_index()
    assert result == (22, "Extreme Fear")


def test_get_fear_greed_index_returns_none_on_missing_fields(credentials_file: Path):
    c = PrefClient(credentials_path=credentials_file)
    payload = {"value": 22}  # missing classification
    with patch("urllib.request.urlopen", return_value=_mock_urlopen(_ok_response(payload))):
        result = c.get_fear_greed_index()
    assert result is None


def test_get_fear_greed_index_rejects_out_of_range_value(credentials_file: Path):
    c = PrefClient(credentials_path=credentials_file)
    payload = {"value": 200, "classification": "Impossible"}
    with patch("urllib.request.urlopen", return_value=_mock_urlopen(_ok_response(payload))):
        result = c.get_fear_greed_index()
    assert result is None


def test_get_fear_greed_index_passes_through_call_failure(credentials_file: Path):
    c = PrefClient(credentials_path=credentials_file)
    with patch("urllib.request.urlopen", side_effect=TimeoutError("slow")):
        result = c.get_fear_greed_index()
    assert result is None
