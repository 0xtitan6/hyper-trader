from unittest.mock import MagicMock

from src.config import DiscoveryConfig
from src.leaders import discover_leaders
from src.liquidiction import Trader


def _disc(top_n=3, min_trades=10, min_volume=100, min_pnl=10) -> DiscoveryConfig:
    return DiscoveryConfig(
        period="7d",
        top_n=top_n,
        min_trades=min_trades,
        min_volume_usd=min_volume,
        min_pnl_usd=min_pnl,
        refresh_seconds=600,
    )


def _t(addr: str, rank: int, pnl=1000, trades=100, volume=10000) -> Trader:
    return Trader(address=addr, rank=rank, pnl=pnl, trades=trades, volume=volume)


def test_filters_by_min_trades():
    client = MagicMock()
    client.top_traders.return_value = [
        _t("0xa", 1, trades=200),
        _t("0xb", 2, trades=5),  # filtered out (below min_trades=10)
        _t("0xc", 3, trades=50),
    ]
    leaders = discover_leaders(client, _disc(top_n=3, min_trades=10))
    assert [t.address for t in leaders] == ["0xa", "0xc"]


def test_filters_by_min_volume_and_pnl():
    client = MagicMock()
    client.top_traders.return_value = [
        _t("0xa", 1, volume=50),  # below min_volume
        _t("0xb", 2, pnl=-5),  # below min_pnl
        _t("0xc", 3),  # passes
    ]
    leaders = discover_leaders(client, _disc(top_n=5))
    assert [t.address for t in leaders] == ["0xc"]


def test_returns_at_most_top_n():
    client = MagicMock()
    client.top_traders.return_value = [_t(f"0x{i}", i) for i in range(20)]
    leaders = discover_leaders(client, _disc(top_n=3))
    assert len(leaders) == 3


def test_empty_input_returns_empty():
    client = MagicMock()
    client.top_traders.return_value = []
    leaders = discover_leaders(client, _disc())
    assert leaders == []


def test_all_filtered_out_returns_empty():
    client = MagicMock()
    client.top_traders.return_value = [_t("0xa", 1, trades=1)]
    leaders = discover_leaders(client, _disc(min_trades=100))
    assert leaders == []


# ---------- always_follow operator override ----------


def _disc_with_always(addrs: list[str], top_n: int = 3) -> DiscoveryConfig:
    return DiscoveryConfig(
        period="7d",
        top_n=top_n,
        min_trades=10,
        min_volume_usd=100,
        min_pnl_usd=10,
        refresh_seconds=600,
        always_follow=addrs,
    )


def test_always_follow_appends_known_address():
    """If the always-follow address is in candidates, include the real Trader."""
    client = MagicMock()
    client.top_traders.return_value = [
        _t("0xa", 1, trades=200),
        _t("0xrwa", 31, pnl=2297, trades=88),  # would normally pass coarse anyway
    ]
    leaders = discover_leaders(client, _disc_with_always(["0xrwa"], top_n=1))
    addrs = [t.address for t in leaders]
    assert "0xa" in addrs
    assert "0xrwa" in addrs
    rwa = next(t for t in leaders if t.address == "0xrwa")
    assert rwa.rank == 31  # full Trader record preserved
    assert rwa.pnl == 2297


def test_always_follow_synthesizes_unknown_address():
    """If the always-follow address is NOT in candidates, synthesize a stub."""
    client = MagicMock()
    client.top_traders.return_value = [_t("0xa", 1, trades=200)]
    leaders = discover_leaders(
        client, _disc_with_always(["0xunknown"], top_n=1)
    )
    addrs = [t.address for t in leaders]
    assert "0xa" in addrs
    assert "0xunknown" in addrs
    stub = next(t for t in leaders if t.address == "0xunknown")
    assert stub.rank == -1  # synthetic marker
    assert stub.pnl == 0.0


def test_always_follow_bypasses_coarse_filter():
    """An always-follow address with low trades/volume/pnl is included anyway."""
    client = MagicMock()
    client.top_traders.return_value = [_t("0xnoise", 99, trades=1, volume=1, pnl=1)]
    leaders = discover_leaders(client, _disc_with_always(["0xnoise"], top_n=3))
    assert [t.address for t in leaders] == ["0xnoise"]


def test_always_follow_dedupes_against_normal_selection():
    """If a leader is selected normally AND in always_follow, only include once."""
    client = MagicMock()
    client.top_traders.return_value = [_t("0xa", 1, trades=200)]
    leaders = discover_leaders(client, _disc_with_always(["0xa"], top_n=3))
    assert [t.address for t in leaders] == ["0xa"]


def test_always_follow_case_insensitive_dedupe():
    """Mixed-case override addresses still dedupe against lowercase candidates."""
    client = MagicMock()
    client.top_traders.return_value = [_t("0xa", 1, trades=200)]
    leaders = discover_leaders(client, _disc_with_always(["0xA"], top_n=3))
    assert len(leaders) == 1


def test_always_follow_added_on_top_of_top_n():
    """always_follow adds to top_n, not into it — operator gets all picks."""
    client = MagicMock()
    client.top_traders.return_value = [_t(f"0x{i}", i, trades=200) for i in range(5)]
    leaders = discover_leaders(client, _disc_with_always(["0xrwa"], top_n=2))
    addrs = [t.address for t in leaders]
    # top 2 from candidates + 1 forced
    assert len(leaders) == 3
    assert "0xrwa" in addrs
