"""Tests for HIP-4 outcome asset ID adapter."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from src.hl_outcome import (
    OUTCOME_ASSET_BASE,
    encode_outcome_asset_id,
    encode_outcome_coin_name,
    register_outcome_assets,
)


def test_encode_outcome_asset_id_btc_binary_yes_no():
    """Per HL docs: outcome_id=2, side=0 → asset 100000020 (#20)
    outcome_id=2, side=1 → asset 100000021 (#21)."""
    assert encode_outcome_asset_id(2, 0) == 100_000_020
    assert encode_outcome_asset_id(2, 1) == 100_000_021


def test_encode_outcome_asset_id_other_outcomes():
    assert encode_outcome_asset_id(0, 0) == OUTCOME_ASSET_BASE
    assert encode_outcome_asset_id(0, 1) == OUTCOME_ASSET_BASE + 1
    assert encode_outcome_asset_id(5, 0) == OUTCOME_ASSET_BASE + 50
    assert encode_outcome_asset_id(5, 1) == OUTCOME_ASSET_BASE + 51
    assert encode_outcome_asset_id(100, 0) == OUTCOME_ASSET_BASE + 1000


def test_encode_outcome_asset_id_rejects_invalid_side():
    with pytest.raises(ValueError, match="side must be 0 or 1"):
        encode_outcome_asset_id(2, 2)
    with pytest.raises(ValueError, match="side must be 0 or 1"):
        encode_outcome_asset_id(2, -1)


def test_encode_outcome_asset_id_rejects_negative_outcome():
    with pytest.raises(ValueError, match="outcome_id must be non-negative"):
        encode_outcome_asset_id(-1, 0)


def test_encode_outcome_coin_name():
    assert encode_outcome_coin_name(2, 0) == "#20"
    assert encode_outcome_coin_name(2, 1) == "#21"
    assert encode_outcome_coin_name(0, 0) == "#0"
    assert encode_outcome_coin_name(7, 1) == "#71"


def test_register_outcome_assets_populates_maps():
    info = MagicMock()
    info.coin_to_asset = {}
    info.name_to_coin = {}
    info.post.return_value = {
        "outcomes": [
            {
                "outcome": 2,
                "name": "Recurring",
                "sideSpecs": [{"name": "Yes"}, {"name": "No"}],
            }
        ]
    }
    n = register_outcome_assets(info)
    assert n == 2
    assert info.coin_to_asset == {"#20": 100_000_020, "#21": 100_000_021}
    assert info.name_to_coin == {"#20": "#20", "#21": "#21"}


def test_register_outcome_assets_handles_multiple_outcomes():
    info = MagicMock()
    info.coin_to_asset = {}
    info.name_to_coin = {}
    info.post.return_value = {
        "outcomes": [
            {"outcome": 2, "sideSpecs": [{"name": "Yes"}, {"name": "No"}]},
            {"outcome": 3, "sideSpecs": [{"name": "Yes"}, {"name": "No"}]},
        ]
    }
    n = register_outcome_assets(info)
    assert n == 4
    assert info.coin_to_asset["#20"] == 100_000_020
    assert info.coin_to_asset["#21"] == 100_000_021
    assert info.coin_to_asset["#30"] == 100_000_030
    assert info.coin_to_asset["#31"] == 100_000_031


def test_register_outcome_assets_handles_empty_response():
    info = MagicMock()
    info.coin_to_asset = {}
    info.name_to_coin = {}
    info.post.return_value = {"outcomes": []}
    assert register_outcome_assets(info) == 0
    assert info.coin_to_asset == {}


def test_register_outcome_assets_handles_fetch_failure():
    info = MagicMock()
    info.coin_to_asset = {}
    info.name_to_coin = {}
    info.post.side_effect = RuntimeError("network down")
    # Should swallow the error and return 0, not raise
    assert register_outcome_assets(info) == 0


def test_register_outcome_assets_idempotent():
    info = MagicMock()
    info.coin_to_asset = {}
    info.name_to_coin = {}
    info.post.return_value = {
        "outcomes": [{"outcome": 2, "sideSpecs": [{"name": "Yes"}, {"name": "No"}]}]
    }
    register_outcome_assets(info)
    snapshot = dict(info.coin_to_asset)
    register_outcome_assets(info)
    # Same map after second call (no duplicates, no corruption)
    assert info.coin_to_asset == snapshot


def test_register_outcome_assets_skips_malformed_entries():
    info = MagicMock()
    info.coin_to_asset = {}
    info.name_to_coin = {}
    info.post.return_value = {
        "outcomes": [
            "not a dict",
            {"outcome": "bad-type"},
            {"sideSpecs": []},  # missing outcome
            {"outcome": 4, "sideSpecs": [{"name": "Yes"}, {"name": "No"}]},  # good
        ]
    }
    n = register_outcome_assets(info)
    assert n == 2  # only the last entry registered
    assert info.coin_to_asset == {"#40": 100_000_040, "#41": 100_000_041}
