"""Tests for src/thesis.py — coin-thesis cache CRUD + lifecycle."""

from __future__ import annotations

import time

import pytest

from src.thesis import (
    SOURCE_MANUAL,
    SOURCE_RULE_BASED,
    STANCE_BEAR,
    STANCE_BULL,
    STANCE_NEUTRAL,
    CoinThesis,
    ThesisCache,
)


@pytest.fixture
def cache(state) -> ThesisCache:
    return ThesisCache(state)


# --- set + get ------------------------------------------------------------


def test_set_persists_and_get_returns(cache: ThesisCache):
    t = cache.set("BTC", STANCE_BULL, 0.7, ttl_s=600, source=SOURCE_MANUAL,
                  evidence={"reason": "oversold"})
    assert t.coin == "BTC"
    assert t.stance == STANCE_BULL
    assert t.confidence == 0.7
    assert t.source == SOURCE_MANUAL
    assert t.evidence == {"reason": "oversold"}

    got = cache.get("BTC")
    assert got is not None
    assert got.coin == "BTC"
    assert got.stance == STANCE_BULL
    assert got.evidence == {"reason": "oversold"}


def test_get_missing_returns_none(cache: ThesisCache):
    assert cache.get("NEVER_SET") is None


def test_set_overwrites_existing(cache: ThesisCache):
    cache.set("BTC", STANCE_BULL, 0.7, ttl_s=600, source=SOURCE_MANUAL)
    cache.set("BTC", STANCE_BEAR, 0.4, ttl_s=300, source=SOURCE_RULE_BASED)
    t = cache.get("BTC")
    assert t is not None
    assert t.stance == STANCE_BEAR
    assert t.confidence == 0.4
    assert t.source == SOURCE_RULE_BASED


def test_evidence_round_trips_through_json(cache: ThesisCache):
    ev = {"fg": 22, "funding_rate": -0.00015, "notes": "contrarian setup"}
    cache.set("ETH", STANCE_BULL, 0.6, ttl_s=600, source=SOURCE_MANUAL, evidence=ev)
    t = cache.get("ETH")
    assert t is not None
    assert t.evidence == ev


# --- validation -----------------------------------------------------------


def test_set_rejects_invalid_stance(cache: ThesisCache):
    with pytest.raises(ValueError, match="invalid stance"):
        cache.set("BTC", "rocket", 0.5, ttl_s=600, source=SOURCE_MANUAL)


def test_set_rejects_confidence_out_of_range(cache: ThesisCache):
    with pytest.raises(ValueError, match="confidence"):
        cache.set("BTC", STANCE_BULL, 1.5, ttl_s=600, source=SOURCE_MANUAL)
    with pytest.raises(ValueError, match="confidence"):
        cache.set("BTC", STANCE_BULL, -0.1, ttl_s=600, source=SOURCE_MANUAL)


def test_set_rejects_non_positive_ttl(cache: ThesisCache):
    with pytest.raises(ValueError, match="ttl_s"):
        cache.set("BTC", STANCE_BULL, 0.5, ttl_s=0, source=SOURCE_MANUAL)
    with pytest.raises(ValueError, match="ttl_s"):
        cache.set("BTC", STANCE_BULL, 0.5, ttl_s=-5, source=SOURCE_MANUAL)


def test_set_rejects_empty_coin(cache: ThesisCache):
    with pytest.raises(ValueError, match="coin"):
        cache.set("", STANCE_BULL, 0.5, ttl_s=600, source=SOURCE_MANUAL)


# --- expiry ---------------------------------------------------------------


def test_get_filters_expired_by_default(cache: ThesisCache, monkeypatch):
    # Set with 100s TTL, then fast-forward time past it
    cache.set("BTC", STANCE_BULL, 0.6, ttl_s=100, source=SOURCE_MANUAL)
    real_time = time.time()
    monkeypatch.setattr(time, "time", lambda: real_time + 200)
    assert cache.get("BTC") is None


def test_get_include_expired_returns_stale(cache: ThesisCache, monkeypatch):
    cache.set("BTC", STANCE_BULL, 0.6, ttl_s=100, source=SOURCE_MANUAL)
    real_time = time.time()
    monkeypatch.setattr(time, "time", lambda: real_time + 200)
    t = cache.get("BTC", include_expired=True)
    assert t is not None
    assert t.is_expired(now=int(real_time + 200))


def test_is_expired_and_ttl_remaining(cache: ThesisCache, monkeypatch):
    cache.set("BTC", STANCE_BULL, 0.6, ttl_s=300, source=SOURCE_MANUAL)
    t = cache.get("BTC")
    assert t is not None
    assert not t.is_expired()
    # 200s remaining (approximately — within slop of test execution time)
    assert 295 <= t.ttl_remaining_s() <= 300


# --- clear + prune --------------------------------------------------------


def test_clear_removes_existing(cache: ThesisCache):
    cache.set("BTC", STANCE_BULL, 0.5, ttl_s=600, source=SOURCE_MANUAL)
    assert cache.clear("BTC") is True
    assert cache.get("BTC") is None


def test_clear_missing_returns_false(cache: ThesisCache):
    assert cache.clear("NEVER_SET") is False


def test_all_active_returns_only_fresh(cache: ThesisCache, monkeypatch):
    cache.set("FRESH1", STANCE_BULL, 0.5, ttl_s=600, source=SOURCE_MANUAL)
    cache.set("FRESH2", STANCE_BEAR, 0.6, ttl_s=600, source=SOURCE_MANUAL)
    cache.set("STALE", STANCE_NEUTRAL, 0.5, ttl_s=10, source=SOURCE_MANUAL)
    real_time = time.time()
    monkeypatch.setattr(time, "time", lambda: real_time + 60)
    active = cache.all_active()
    coins = {t.coin for t in active}
    assert coins == {"FRESH1", "FRESH2"}


def test_all_active_orders_by_freshness(cache: ThesisCache):
    """Most recently generated first."""
    cache.set("OLD", STANCE_BULL, 0.5, ttl_s=600, source=SOURCE_MANUAL)
    time.sleep(1.05)  # ensure distinct generated_at seconds
    cache.set("NEW", STANCE_BEAR, 0.5, ttl_s=600, source=SOURCE_MANUAL)
    active = cache.all_active()
    assert [t.coin for t in active] == ["NEW", "OLD"]


def test_prune_expired_removes_only_stale(cache: ThesisCache, monkeypatch):
    cache.set("KEEP", STANCE_BULL, 0.5, ttl_s=600, source=SOURCE_MANUAL)
    cache.set("DROP1", STANCE_BEAR, 0.5, ttl_s=10, source=SOURCE_MANUAL)
    cache.set("DROP2", STANCE_NEUTRAL, 0.5, ttl_s=10, source=SOURCE_MANUAL)
    real_time = time.time()
    monkeypatch.setattr(time, "time", lambda: real_time + 60)
    removed = cache.prune_expired()
    assert removed == 2
    assert cache.get("KEEP", include_expired=False) is not None
    assert cache.get("DROP1", include_expired=True) is None  # actually removed from DB
    assert cache.get("DROP2", include_expired=True) is None


def test_prune_when_nothing_expired(cache: ThesisCache):
    cache.set("BTC", STANCE_BULL, 0.5, ttl_s=600, source=SOURCE_MANUAL)
    assert cache.prune_expired() == 0


# --- CoinThesis dataclass --------------------------------------------------


def test_cointhesis_dataclass_is_frozen():
    t = CoinThesis(
        coin="BTC",
        stance=STANCE_BULL,
        confidence=0.5,
        generated_at=int(time.time()),
        expires_at=int(time.time()) + 600,
        source=SOURCE_MANUAL,
    )
    with pytest.raises(Exception):  # FrozenInstanceError
        t.confidence = 0.9  # type: ignore[misc]
