"""Tests for src/funding_history.py — realized funding-payment tracking."""

from __future__ import annotations

import time
from unittest.mock import MagicMock

from src.funding_history import FundingHistory


def _event(time_ms: int, coin: str, usdc: float, szi: float = 0.0, rate: float = 0.0) -> dict:
    return {
        "time": time_ms,
        "hash": "0x" + "0" * 64,
        "delta": {
            "type": "funding",
            "coin": coin,
            "usdc": str(usdc),
            "szi": str(szi),
            "fundingRate": str(rate),
            "nSamples": 12,
        },
    }


# --- state schema -----------------------------------------------------------


def test_state_records_funding_event(state):
    """New table exists, primary key on (time_ms, coin)."""
    inserted = state.record_funding_event(1700000000000, "BTC", 0.123, 0.5, 0.0001)
    assert inserted is True
    # Duplicate insert returns False (dedupe by PK)
    dup = state.record_funding_event(1700000000000, "BTC", 0.123, 0.5, 0.0001)
    assert dup is False


def test_state_funding_total_since(state):
    state.record_funding_event(1700000000000, "BTC", 0.10)
    state.record_funding_event(1700001000000, "ETH", -0.05)
    state.record_funding_event(1700002000000, "BTC", 0.20)
    total = state.funding_total_since(1700000000)  # seconds, includes all
    assert abs(total - 0.25) < 1e-9


def test_state_funding_by_coin_since(state):
    state.record_funding_event(1700000000000, "BTC", 0.10)
    state.record_funding_event(1700001000000, "BTC", 0.05)
    state.record_funding_event(1700001000000, "ETH", -0.03)
    by_coin = state.funding_by_coin_since(1700000000)
    assert abs(by_coin["BTC"] - 0.15) < 1e-9
    assert abs(by_coin["ETH"] - (-0.03)) < 1e-9


def test_state_funding_total_excludes_older(state):
    state.record_funding_event(1700000000000, "BTC", 0.10)
    state.record_funding_event(1800000000000, "BTC", 0.50)
    # Cutoff between the two
    total = state.funding_total_since(1750000000)
    assert abs(total - 0.50) < 1e-9


def test_state_last_funding_event_ms(state):
    assert state.last_funding_event_ms() is None
    state.record_funding_event(1700000000000, "BTC", 0.10)
    state.record_funding_event(1700001000000, "BTC", 0.20)
    state.record_funding_event(1700002000000, "ETH", -0.05)
    assert state.last_funding_event_ms() == 1700002000000


# --- FundingHistory.poll ---------------------------------------------------


def test_poll_cold_start_uses_lookback(state):
    """First poll (empty table) requests `initial_lookback_days` of history."""
    fh = FundingHistory(state, initial_lookback_days=7)
    info = MagicMock()
    info.post.return_value = []
    fh.poll(info, "0xACC")
    info.post.assert_called_once()
    payload = info.post.call_args.args[1]
    assert payload["type"] == "userFunding"
    assert payload["user"] == "0xacc"
    expected_since = int((time.time() - 7 * 86400) * 1000)
    # Within 5s slop (tests are fast but be tolerant)
    assert abs(payload["startTime"] - expected_since) < 5000


def test_poll_incremental_uses_last_event_plus_one(state):
    """Second poll uses last recorded ts + 1 as the cursor (no double-count)."""
    state.record_funding_event(1700000000000, "BTC", 0.10)
    fh = FundingHistory(state)
    info = MagicMock()
    info.post.return_value = []
    fh.poll(info, "0xACC")
    payload = info.post.call_args.args[1]
    assert payload["startTime"] == 1700000000001


def test_poll_inserts_new_events(state):
    fh = FundingHistory(state)
    info = MagicMock()
    info.post.return_value = [
        _event(1700000000000, "BTC", 0.10, szi=0.5, rate=0.0001),
        _event(1700001000000, "ETH", -0.05, szi=-1.0, rate=-0.00002),
    ]
    inserted = fh.poll(info, "0xACC")
    assert inserted == 2
    assert abs(state.funding_total_since(1700000000) - 0.05) < 1e-9


def test_poll_dedupes_existing_events(state):
    state.record_funding_event(1700000000000, "BTC", 0.10)
    fh = FundingHistory(state)
    info = MagicMock()
    info.post.return_value = [
        _event(1700000000000, "BTC", 0.10),  # already in DB
        _event(1700001000000, "ETH", -0.05),  # new
    ]
    inserted = fh.poll(info, "0xACC")
    assert inserted == 1


def test_poll_tolerates_non_list_response(state):
    fh = FundingHistory(state)
    info = MagicMock()
    info.post.return_value = {"oops": "shape"}
    inserted = fh.poll(info, "0xACC")
    assert inserted == 0


def test_poll_tolerates_fetch_exception(state):
    fh = FundingHistory(state)
    info = MagicMock()
    info.post.side_effect = RuntimeError("network down")
    inserted = fh.poll(info, "0xACC")
    assert inserted == 0


def test_poll_skips_non_funding_delta_types(state):
    """HL's userFunding endpoint occasionally returns mixed delta types
    (e.g. settlement entries). Only `type=funding` events should be recorded."""
    fh = FundingHistory(state)
    info = MagicMock()
    info.post.return_value = [
        _event(1700000000000, "BTC", 0.10),
        {"time": 1700001000000, "delta": {"type": "settlement", "usdc": "99"}},
        _event(1700002000000, "ETH", -0.05),
    ]
    inserted = fh.poll(info, "0xACC")
    assert inserted == 2  # not 3


def test_poll_skips_malformed_events(state):
    fh = FundingHistory(state)
    info = MagicMock()
    info.post.return_value = [
        _event(1700000000000, "BTC", 0.10),
        {"time": 1700001000000, "delta": "not_a_dict"},
        {"time": "not_an_int", "delta": {"type": "funding", "coin": "ETH", "usdc": "0.1"}},
        {"time": 1700003000000, "delta": {"type": "funding", "coin": "", "usdc": "0.1"}},  # empty coin
    ]
    inserted = fh.poll(info, "0xACC")
    assert inserted == 1  # only the first


def test_poll_address_lowercased_in_request(state):
    fh = FundingHistory(state)
    info = MagicMock()
    info.post.return_value = []
    fh.poll(info, "0xABCDEF1234567890")
    payload = info.post.call_args.args[1]
    assert payload["user"] == "0xabcdef1234567890"
