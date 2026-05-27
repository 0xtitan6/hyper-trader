"""Minimal HTTP client for the Preference MCP (pref.trade) — used by the
bot for crypto sentiment + on-chain data that HL doesn't expose.

This is a deliberately small surface: just `call_tool(name, arguments)`
that hides the JSON-RPC envelope and Bearer-token auth. Nothing fancy.

Key lives at `~/.config/preference/credentials.json` (chmod 600); we read
it lazily on first call so the bot can boot even if PREF is misconfigured.
A failed call returns `None` — caller treats PREF as a soft dependency
(missing sentiment is acceptable; missing fills is not).

See `memory/reference_pref_mcp.md` for onboarding + claim-status notes.
"""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

DEFAULT_CREDENTIALS_PATH = Path.home() / ".config" / "preference" / "credentials.json"
DEFAULT_ENDPOINT = "https://pref.trade/mcp"
DEFAULT_TIMEOUT_S = 10.0


class PrefClient:
    """Thin HTTP wrapper around the Preference MCP JSON-RPC endpoint."""

    def __init__(
        self,
        credentials_path: Path | str = DEFAULT_CREDENTIALS_PATH,
        endpoint: str = DEFAULT_ENDPOINT,
        timeout_s: float = DEFAULT_TIMEOUT_S,
    ):
        self.credentials_path = Path(credentials_path)
        self.endpoint = endpoint
        self.timeout_s = timeout_s
        self._api_key: str | None = None

    def _load_key(self) -> str | None:
        if self._api_key is not None:
            return self._api_key
        if not self.credentials_path.exists():
            log.warning(
                "PrefClient: credentials file missing at %s — PREF disabled",
                self.credentials_path,
            )
            return None
        try:
            data = json.loads(self.credentials_path.read_text())
            key = data.get("api_key")
            if not key:
                log.warning("PrefClient: credentials file has no api_key field")
                return None
            self._api_key = key
            return key
        except (OSError, json.JSONDecodeError):
            log.exception("PrefClient: failed to load credentials")
            return None

    def call_tool(self, name: str, arguments: dict | None = None) -> dict | None:
        """Invoke one MCP tool by name. Returns the parsed `result.content[0].text`
        as a dict (PREF tools wrap their payload in a single text block of JSON),
        or None on any error.

        Returning None instead of raising is intentional — PREF is a soft
        dependency. Callers (e.g. ThesisGenerator) tolerate missing data
        by falling back to their own signals.
        """
        key = self._load_key()
        if key is None:
            return None
        payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments or {}},
        }
        body = json.dumps(payload).encode()
        req = urllib.request.Request(
            self.endpoint,
            data=body,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json, text/event-stream",
                "Authorization": f"Bearer {key}",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout_s) as resp:
                raw = resp.read().decode("utf-8", errors="replace")
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError):
            log.exception("PrefClient: HTTP call to %s failed", name)
            return None
        return self._parse_response(name, raw)

    @staticmethod
    def _parse_response(name: str, raw: str) -> dict | None:
        """Extract the inner JSON payload from the JSON-RPC envelope.

        PREF tools wrap their data inside `result.content[0].text` as
        a JSON string. We dig down to it. Returns None on any shape mismatch
        rather than raising — same soft-dependency contract."""
        try:
            envelope = json.loads(raw)
        except json.JSONDecodeError:
            log.warning("PrefClient: non-JSON response for %s: %s", name, raw[:200])
            return None
        if not isinstance(envelope, dict):
            return None
        # JSON-RPC error path
        if "error" in envelope:
            log.warning("PrefClient: tool %s returned error: %s", name, envelope.get("error"))
            return None
        result = envelope.get("result")
        if not isinstance(result, dict):
            return None
        # PREF also exposes structuredContent in some tools — prefer that if present
        sc = result.get("structuredContent")
        if isinstance(sc, dict):
            return sc
        # Otherwise pull from content[0].text
        content = result.get("content")
        if not isinstance(content, list) or not content:
            return None
        first = content[0]
        if not isinstance(first, dict):
            return None
        text = first.get("text")
        if not isinstance(text, str):
            return None
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            log.warning("PrefClient: tool %s text payload is not JSON: %s", name, text[:200])
            return None

    def get_fear_greed_index(self) -> tuple[int, str] | None:
        """Convenience wrapper for the most-used signal — crypto fear & greed.
        Returns (value 0-100, classification) or None on failure.

        The underlying tool `crypto.get_fear_greed_index` returns
        `{"value": int, "classification": str, "timestamp": ..., "time_until_update": ...}`.
        Source: alternative.me — updates ~once daily, so polling more
        often than ~1 hour is wasteful (and we cache at the call site)."""
        result = self.call_tool("crypto.get_fear_greed_index")
        if result is None:
            return None
        try:
            value = int(result["value"])
            classification = str(result["classification"])
        except (KeyError, TypeError, ValueError):
            log.warning("PrefClient: unexpected fear_greed payload: %s", result)
            return None
        if not (0 <= value <= 100):
            log.warning("PrefClient: fear_greed value out of range: %d", value)
            return None
        return value, classification
