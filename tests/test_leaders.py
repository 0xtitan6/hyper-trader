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
