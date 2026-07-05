from unittest.mock import MagicMock

import pytest

from src.errors import MarketMetaError
from src.market_meta import MarketMeta


@pytest.fixture
def info():
    info = MagicMock()
    info.meta.return_value = {
        "universe": [
            {"name": "BTC", "szDecimals": 5},
            {"name": "ETH", "szDecimals": 4},
            {"name": "WIF", "szDecimals": 0},
        ]
    }
    info.spot_meta.return_value = {
        "universe": [
            {"name": "PURR/USDC", "tokens": [1, 0]},
            {"name": "@1", "tokens": [2, 0]},
        ],
        "tokens": [
            {"name": "USDC", "szDecimals": 8, "index": 0},
            {"name": "PURR", "szDecimals": 0, "index": 1},
            {"name": "FOO", "szDecimals": 4, "index": 2},
        ],
    }
    return info


def test_load_caches_decimals(info):
    mm = MarketMeta(info)
    mm.load()
    assert mm._size_decimals_for("BTC") == 5
    assert mm._size_decimals_for("ETH") == 4
    assert mm._size_decimals_for("WIF") == 0
    assert mm._size_decimals_for("PURR/USDC") == 0  # base = PURR
    assert mm._size_decimals_for("@1") == 4  # base = FOO


def test_load_idempotent(info):
    mm = MarketMeta(info)
    mm.load()
    mm.load()
    assert info.meta.call_count == 1
    assert info.spot_meta.call_count == 1


def test_unknown_perp_uses_default(info):
    mm = MarketMeta(info)
    mm.load()
    assert mm._size_decimals_for("UNKNOWN") == 4  # default


def test_outcomes_round_to_integer(info):
    mm = MarketMeta(info)
    mm.load()
    assert mm.round_size("#11", 12.7) == 12.0
    assert mm.round_size("#10", 0.5) == 0.0
    assert mm.round_size("+11", 99.999) == 99.0


def test_perp_rounds_to_szdecimals(info):
    mm = MarketMeta(info)
    mm.load()
    # BTC szDecimals=5
    assert mm.round_size("BTC", 0.0123456789) == 0.01234
    # ETH szDecimals=4
    assert mm.round_size("ETH", 0.123456) == 0.1234
    # WIF szDecimals=0
    assert mm.round_size("WIF", 12.7) == 12.0


def test_round_size_zero_or_negative(info):
    mm = MarketMeta(info)
    mm.load()
    assert mm.round_size("BTC", 0) == 0
    assert mm.round_size("BTC", -1) == 0


def test_round_size_below_min_rounds_to_zero(info):
    mm = MarketMeta(info)
    mm.load()
    assert mm.round_size("BTC", 0.000001) == 0.0  # below 5 decimals → 0


def test_round_price_5_sig_figs(info):
    mm = MarketMeta(info)
    mm.load()
    assert mm.round_price(0.5427) == 0.5427
    assert mm.round_price(0.54271234) == 0.54271
    assert mm.round_price(65000.123) == 65000.0
    assert mm.round_price(1.234567) == 1.2346


def test_round_price_zero_negative(info):
    mm = MarketMeta(info)
    mm.load()
    assert mm.round_price(0) == 0
    assert mm.round_price(-1) == -1


def test_perp_meta_failure_raises():
    info = MagicMock()
    info.meta.side_effect = RuntimeError("upstream down")
    mm = MarketMeta(info)
    with pytest.raises(MarketMetaError, match="perp meta"):
        mm.load()


def test_spot_meta_failure_raises():
    info = MagicMock()
    info.meta.return_value = {"universe": []}
    info.spot_meta.side_effect = RuntimeError("upstream down")
    mm = MarketMeta(info)
    with pytest.raises(MarketMetaError, match="spot meta"):
        mm.load()


def test_empty_universe_safe(info):
    info.meta.return_value = {}
    info.spot_meta.return_value = {}
    mm = MarketMeta(info)
    mm.load()  # should not raise
    assert mm._size_decimals_for("ANYTHING") == 4


# --- HIP-3 (builder-deployed) perp dex szDecimals ---------------------------


def _info_with_hip3(base_info, *, dexes, meta_by_dex):
    """Route info.post() so perpDexs / per-dex meta return HIP-3 data."""

    def post(path, payload):
        t = payload.get("type")
        if t == "perpDexs":
            return dexes
        if t == "meta":
            return meta_by_dex.get(payload.get("dex"))
        return None

    base_info.post.side_effect = post
    return base_info


def test_hip3_szdecimals_loaded_from_per_dex_meta(info):
    # Regression: before the fix, xyz:NVDA/xyz:INTC were absent from the
    # cache and _size_decimals_for fell back to the perp default of 4, so
    # round_size produced 4-decimal sizes that HL rejects as "invalid size".
    _info_with_hip3(
        info,
        dexes=[None, {"name": "xyz"}],
        meta_by_dex={
            "xyz": {
                "universe": [
                    {"name": "xyz:NVDA", "szDecimals": 3},
                    {"name": "xyz:INTC", "szDecimals": 2},
                    {"name": "xyz:TSLA", "szDecimals": 3},
                ]
            }
        },
    )
    mm = MarketMeta(info)
    mm.load()

    assert mm._size_decimals_for("xyz:NVDA") == 3
    assert mm._size_decimals_for("xyz:INTC") == 2
    assert mm._size_decimals_for("xyz:TSLA") == 3

    # The exact documented failure: 0.12345 must round to 3 dp, not 4.
    assert mm.round_size("xyz:NVDA", 0.12345) == 0.123
    assert mm.round_size("xyz:INTC", 0.0806) == 0.08


def test_hip3_multiple_dexes_registered(info):
    _info_with_hip3(
        info,
        dexes=[None, {"name": "xyz"}, {"name": "flx"}],
        meta_by_dex={
            "xyz": {"universe": [{"name": "xyz:NVDA", "szDecimals": 3}]},
            "flx": {"universe": [{"name": "flx:OIL", "szDecimals": 1}]},
        },
    )
    mm = MarketMeta(info)
    mm.load()
    assert mm._size_decimals_for("xyz:NVDA") == 3
    assert mm._size_decimals_for("flx:OIL") == 1


def test_hip3_fetch_failure_does_not_break_base_load(info):
    # HIP-3 metadata is best-effort; a failure must not break perp/spot.
    info.post.side_effect = RuntimeError("perpDexs down")
    mm = MarketMeta(info)
    mm.load()  # should not raise
    assert mm._size_decimals_for("BTC") == 5  # base perp still cached
    # Unknown HIP-3 coin still falls back to perp default.
    assert mm._size_decimals_for("xyz:NVDA") == 4


def test_hip3_non_list_perpdexs_safe(info):
    info.post.side_effect = lambda path, payload: {"not": "a list"}
    mm = MarketMeta(info)
    mm.load()  # should not raise
    assert mm._size_decimals_for("BTC") == 5
