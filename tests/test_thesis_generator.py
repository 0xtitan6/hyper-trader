"""Tests for src/thesis_generator.py — rule-based thesis writer."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from src.thesis import (
    SOURCE_RULE_BASED,
    STANCE_BEAR,
    STANCE_BULL,
    STANCE_NEUTRAL,
    ThesisCache,
)
from src.thesis_generator import (
    CONFIDENCE_BOTH_AGREE,
    CONFIDENCE_CONCORDANCE_ONLY,
    CONFIDENCE_FUNDING_ONLY,
    ThesisGenerator,
)


@pytest.fixture
def cache(state) -> ThesisCache:
    return ThesisCache(state)


def _mock_info(
    leader_positions: dict[str, dict[str, float]] | None = None,
    funding_by_coin: dict[str, float] | None = None,
) -> MagicMock:
    """Build a mock Info that returns canned leader positions + funding ctx."""
    info = MagicMock()

    def user_state(address: str) -> dict:
        book = (leader_positions or {}).get(address.lower(), {})
        return {
            "assetPositions": [
                {"position": {"coin": c, "szi": str(s)}} for c, s in book.items()
            ]
        }

    info.user_state.side_effect = user_state

    # Build a meta_and_asset_ctxs response with funding rates per coin
    if funding_by_coin is not None:
        universe = [{"name": c} for c in funding_by_coin]
        # HL funding is hourly rate. APR = hourly × 24 × 365 × 100.
        # So to inject an APR target, set hourly = apr_pct / (24*365*100)
        ctxs = [{"funding": str(apr / (24 * 365 * 100))} for apr in funding_by_coin.values()]
        info.meta_and_asset_ctxs.return_value = [{"universe": universe}, ctxs]
    else:
        info.meta_and_asset_ctxs.return_value = [{"universe": []}, []]
    return info


def _make_gen(state, cache, leader_positions, funding=None, addrs=None) -> ThesisGenerator:
    addrs = addrs or list(leader_positions.keys())
    info = _mock_info(leader_positions=leader_positions, funding_by_coin=funding)
    return ThesisGenerator(info=info, state=state, cache=cache, leader_addresses=addrs, ttl_s=600)


# --- concordance only ---------------------------------------------------


def test_concordance_two_longs_writes_bull(state, cache):
    """≥2 leaders LONG triggers BULL with concordance-only confidence."""
    leaders = {
        "0xa": {"BTC": 0.5},
        "0xb": {"BTC": 1.0},
        "0xc": {},
    }
    gen = _make_gen(state, cache, leaders)
    out = gen.run_cycle()
    assert "BTC" in out
    assert out["BTC"].stance == STANCE_BULL
    assert out["BTC"].confidence == CONFIDENCE_CONCORDANCE_ONLY
    assert out["BTC"].source == SOURCE_RULE_BASED
    assert out["BTC"].evidence["longs"] == 2
    assert out["BTC"].evidence["shorts"] == 0


def test_concordance_two_shorts_writes_bear(state, cache):
    leaders = {
        "0xa": {"ETH": -0.5},
        "0xb": {"ETH": -1.0},
    }
    gen = _make_gen(state, cache, leaders)
    out = gen.run_cycle()
    assert out["ETH"].stance == STANCE_BEAR
    assert out["ETH"].confidence == CONFIDENCE_CONCORDANCE_ONLY


def test_single_leader_no_concordance(state, cache):
    """One leader alone doesn't trigger concordance — neutral by default."""
    leaders = {"0xa": {"BTC": 0.5}}
    gen = _make_gen(state, cache, leaders)
    out = gen.run_cycle()
    assert "BTC" in out
    assert out["BTC"].stance == STANCE_NEUTRAL


def test_mixed_directions_no_concordance(state, cache):
    """1 long + 1 short = no clear signal → neutral."""
    leaders = {
        "0xa": {"BTC": 0.5},
        "0xb": {"BTC": -1.0},
    }
    gen = _make_gen(state, cache, leaders)
    out = gen.run_cycle()
    assert out["BTC"].stance == STANCE_NEUTRAL


# --- funding only -------------------------------------------------------


def test_funding_extreme_negative_apr_writes_bull(state, cache):
    """One leader (no concordance) + funding APR -15% (below -10% threshold) → BULL."""
    leaders = {"0xa": {"BTC": 0.5}}
    gen = _make_gen(state, cache, leaders, funding={"BTC": -15.0})
    out = gen.run_cycle()
    assert out["BTC"].stance == STANCE_BULL
    assert out["BTC"].confidence == CONFIDENCE_FUNDING_ONLY
    assert out["BTC"].evidence["rule"] == "funding_bull"


def test_funding_extreme_positive_apr_writes_bear(state, cache):
    """One leader + funding APR +60% (above +50% threshold) → BEAR."""
    leaders = {"0xa": {"BTC": 0.5}}
    gen = _make_gen(state, cache, leaders, funding={"BTC": 60.0})
    out = gen.run_cycle()
    assert out["BTC"].stance == STANCE_BEAR
    assert out["BTC"].confidence == CONFIDENCE_FUNDING_ONLY


def test_funding_within_normal_range_neutral(state, cache):
    leaders = {"0xa": {"BTC": 0.5}}
    gen = _make_gen(state, cache, leaders, funding={"BTC": 5.0})  # mild positive
    out = gen.run_cycle()
    assert out["BTC"].stance == STANCE_NEUTRAL


# --- both agree → max confidence ----------------------------------------


def test_concordance_and_funding_both_bull_max_confidence(state, cache):
    leaders = {
        "0xa": {"BTC": 0.5},
        "0xb": {"BTC": 1.0},
    }
    gen = _make_gen(state, cache, leaders, funding={"BTC": -15.0})
    out = gen.run_cycle()
    assert out["BTC"].stance == STANCE_BULL
    assert out["BTC"].confidence == CONFIDENCE_BOTH_AGREE
    assert "concordance_bull + funding_bull" in out["BTC"].evidence["rule"]


def test_concordance_and_funding_both_bear_max_confidence(state, cache):
    leaders = {
        "0xa": {"ETH": -0.5},
        "0xb": {"ETH": -1.0},
    }
    gen = _make_gen(state, cache, leaders, funding={"ETH": 60.0})
    out = gen.run_cycle()
    assert out["ETH"].stance == STANCE_BEAR
    assert out["ETH"].confidence == CONFIDENCE_BOTH_AGREE


# --- coin filtering -----------------------------------------------------


def test_filters_outcomes_and_spot(state, cache):
    """Coins prefixed # / + (outcomes) or @ / containing / (spot) excluded."""
    leaders = {
        "0xa": {"BTC": 0.5, "#42": 100, "+11": 50, "@230": 5, "USDC/USDH": 1},
        "0xb": {"BTC": 0.5, "#42": -100, "+11": -50, "@230": -5},
    }
    gen = _make_gen(state, cache, leaders)
    out = gen.run_cycle()
    assert "BTC" in out
    assert "#42" not in out
    assert "+11" not in out
    assert "@230" not in out
    assert "USDC/USDH" not in out


def test_zero_size_positions_excluded(state, cache):
    """Leaders with zero-size entries shouldn't contribute to concordance."""
    leaders = {
        "0xa": {"BTC": 0.5},
        "0xb": {"BTC": 0.0},  # closed/zero — ignored
        "0xc": {"BTC": 1.0},
    }
    gen = _make_gen(state, cache, leaders)
    out = gen.run_cycle()
    # Only 2 actual longs — meets threshold of 2
    assert out["BTC"].stance == STANCE_BULL


# --- failure handling ---------------------------------------------------


def test_all_leader_fetches_fail_returns_empty(state, cache):
    info = MagicMock()
    info.user_state.side_effect = OSError("network down")
    info.meta_and_asset_ctxs.return_value = [{"universe": []}, []]
    gen = ThesisGenerator(info=info, state=state, cache=cache, leader_addresses=["0xa"], ttl_s=600)
    out = gen.run_cycle()
    assert out == {}


def test_one_leader_fetch_fails_others_succeed(state, cache):
    """A single failed leader fetch shouldn't block thesis generation."""
    info = MagicMock()

    def user_state(addr):
        if addr.lower() == "0xa":
            raise OSError("transient")
        return {"assetPositions": [
            {"position": {"coin": "BTC", "szi": "1.0"}},
        ]}

    info.user_state.side_effect = user_state
    info.meta_and_asset_ctxs.return_value = [{"universe": []}, []]
    gen = ThesisGenerator(info=info, state=state, cache=cache, leader_addresses=["0xa", "0xb"], ttl_s=600)
    out = gen.run_cycle()
    # Only 0xb returned positions, only 1 long → no concordance → neutral
    assert "BTC" in out
    assert out["BTC"].stance == STANCE_NEUTRAL


def test_funding_fetch_failure_degrades_to_concordance_only(state, cache):
    """If meta_and_asset_ctxs raises, we still write theses from concordance."""
    info = MagicMock()
    info.user_state.return_value = {
        "assetPositions": [{"position": {"coin": "BTC", "szi": "0.5"}}]
    }
    info.meta_and_asset_ctxs.side_effect = RuntimeError("HL down")
    gen = ThesisGenerator(info=info, state=state, cache=cache,
                          leader_addresses=["0xa", "0xb"], ttl_s=600)
    out = gen.run_cycle()
    # Both leaders return same positions → 2 longs → BULL on concordance
    assert out["BTC"].stance == STANCE_BULL
    # No funding evidence since fetch failed
    assert "funding_apr" not in out["BTC"].evidence


# --- update_leaders -----------------------------------------------------


def test_update_leaders_replaces_set(state, cache):
    gen = _make_gen(state, cache, {"0xa": {"BTC": 0.5}}, addrs=["0xa"])
    assert gen.leader_addresses == ["0xa"]
    gen.update_leaders(["0xB", "0xc"])  # mixed case — should lowercase
    assert gen.leader_addresses == ["0xb", "0xc"]


# --- TTL + cache integration --------------------------------------------


def test_writes_persist_to_cache_with_ttl(state, cache):
    leaders = {"0xa": {"BTC": 0.5}, "0xb": {"BTC": 1.0}}
    gen = _make_gen(state, cache, leaders)
    gen.ttl_s = 12345
    gen.run_cycle()
    t = cache.get("BTC")
    assert t is not None
    assert t.stance == STANCE_BULL
    assert 12340 <= t.ttl_remaining_s() <= 12345


def test_consecutive_runs_overwrite_thesis(state, cache):
    """Calling run_cycle twice with different leader books updates the thesis."""
    # Cycle 1: both long → BULL
    leaders1 = {"0xa": {"BTC": 0.5}, "0xb": {"BTC": 1.0}}
    gen = _make_gen(state, cache, leaders1)
    gen.run_cycle()
    assert cache.get("BTC").stance == STANCE_BULL  # type: ignore[union-attr]

    # Cycle 2: both flipped short → BEAR
    leaders2 = {"0xa": {"BTC": -0.5}, "0xb": {"BTC": -1.0}}
    gen.info = _mock_info(leaders2)
    gen.run_cycle()
    assert cache.get("BTC").stance == STANCE_BEAR  # type: ignore[union-attr]


# --- fear/greed enrichment via PREF client ------------------------------


def test_fear_greed_attached_to_evidence_when_pref_client_provided(state, cache):
    """PREF returns (22, 'Extreme Fear') → both fields land in evidence."""
    pref = MagicMock()
    pref.get_fear_greed_index.return_value = (22, "Extreme Fear")
    leaders = {"0xa": {"BTC": 0.5}, "0xb": {"BTC": 1.0}}
    info = _mock_info(leader_positions=leaders)
    from src.thesis_generator import ThesisGenerator
    gen = ThesisGenerator(info=info, state=state, cache=cache,
                          leader_addresses=["0xa", "0xb"], ttl_s=600,
                          pref_client=pref)
    out = gen.run_cycle()
    assert out["BTC"].evidence["fear_greed"] == 22
    assert out["BTC"].evidence["fear_greed_classification"] == "Extreme Fear"
    # PREF was called exactly once even though there's one coin in this cycle
    # (per-cycle cache, not per-coin)
    assert pref.get_fear_greed_index.call_count == 1


def test_fear_greed_cache_reused_within_ttl(state, cache):
    """Second run_cycle within TTL window does NOT call PREF again."""
    pref = MagicMock()
    pref.get_fear_greed_index.return_value = (50, "Neutral")
    leaders = {"0xa": {"BTC": 0.5}, "0xb": {"BTC": 1.0}}
    info = _mock_info(leader_positions=leaders)
    from src.thesis_generator import ThesisGenerator
    gen = ThesisGenerator(info=info, state=state, cache=cache,
                          leader_addresses=["0xa", "0xb"], ttl_s=600,
                          pref_client=pref)
    gen.run_cycle()
    gen.run_cycle()
    # Only the first cycle's call hit PREF — second used cached value
    assert pref.get_fear_greed_index.call_count == 1


def test_fear_greed_cache_refreshes_after_ttl(state, cache, monkeypatch):
    """After FEAR_GREED_TTL_S elapses, PREF gets called again."""
    pref = MagicMock()
    pref.get_fear_greed_index.return_value = (60, "Greed")
    leaders = {"0xa": {"BTC": 0.5}, "0xb": {"BTC": 1.0}}
    info = _mock_info(leader_positions=leaders)
    from src.thesis_generator import ThesisGenerator
    gen = ThesisGenerator(info=info, state=state, cache=cache,
                          leader_addresses=["0xa", "0xb"], ttl_s=600,
                          pref_client=pref)
    gen.run_cycle()
    # Fast-forward past TTL
    import time as time_module
    real_time = time_module.time()
    monkeypatch.setattr(time_module, "time", lambda: real_time + 2000)
    gen.run_cycle()
    assert pref.get_fear_greed_index.call_count == 2


def test_no_fear_greed_when_pref_client_missing(state, cache):
    """Generator with pref_client=None just omits the sentiment evidence."""
    leaders = {"0xa": {"BTC": 0.5}, "0xb": {"BTC": 1.0}}
    info = _mock_info(leader_positions=leaders)
    from src.thesis_generator import ThesisGenerator
    gen = ThesisGenerator(info=info, state=state, cache=cache,
                          leader_addresses=["0xa", "0xb"], ttl_s=600,
                          pref_client=None)
    out = gen.run_cycle()
    assert "fear_greed" not in out["BTC"].evidence


def test_fear_greed_call_failure_does_not_block_thesis(state, cache):
    """PREF returns None — generator still writes thesis without sentiment."""
    pref = MagicMock()
    pref.get_fear_greed_index.return_value = None
    leaders = {"0xa": {"BTC": 0.5}, "0xb": {"BTC": 1.0}}
    info = _mock_info(leader_positions=leaders)
    from src.thesis_generator import ThesisGenerator
    gen = ThesisGenerator(info=info, state=state, cache=cache,
                          leader_addresses=["0xa", "0xb"], ttl_s=600,
                          pref_client=pref)
    out = gen.run_cycle()
    assert out["BTC"].stance == STANCE_BULL  # rest of logic unaffected
    assert "fear_greed" not in out["BTC"].evidence


def test_fear_greed_falls_back_to_stale_cache_when_refresh_fails(state, cache, monkeypatch):
    """If a refresh fails but we have a cached value, use the cached value
    rather than dropping the signal entirely."""
    pref = MagicMock()
    pref.get_fear_greed_index.side_effect = [(40, "Fear"), None]  # 2nd call fails
    leaders = {"0xa": {"BTC": 0.5}, "0xb": {"BTC": 1.0}}
    info = _mock_info(leader_positions=leaders)
    from src.thesis_generator import ThesisGenerator
    gen = ThesisGenerator(info=info, state=state, cache=cache,
                          leader_addresses=["0xa", "0xb"], ttl_s=600,
                          pref_client=pref)
    gen.run_cycle()
    # Fast forward past TTL to trigger refresh, which fails
    import time as time_module
    real_time = time_module.time()
    monkeypatch.setattr(time_module, "time", lambda: real_time + 2000)
    out = gen.run_cycle()
    # Cached value still present in evidence
    assert out["BTC"].evidence["fear_greed"] == 40
