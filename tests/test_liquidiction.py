import json
from urllib.parse import parse_qs, urlparse

import pytest
import requests
import responses

from src.liquidiction import LiquidictionClient


@pytest.fixture
def client() -> LiquidictionClient:
    return LiquidictionClient("https://liquidiction.xyz")


def _mk_row_idx(idx: int) -> dict:
    return {
        "address": "0x" + format(idx + 1, "x").rjust(40, "0"),
        "rank": idx + 1,
        "pnl": 1000.0 + idx,
        "trades": 100 + idx,
        "volume": 10_000.0 + idx,
    }


def _paged_callback(total: int):
    def cb(req):
        qs = parse_qs(urlparse(req.url).query)
        offset = int(qs["offset"][0])
        limit = int(qs["limit"][0])
        rows = [_mk_row_idx(i) for i in range(offset, min(offset + limit, total))]
        return 200, {}, json.dumps({"traders": rows, "totalTraders": total})

    return cb


@responses.activate
def test_top_traders_paginates(client: LiquidictionClient):
    responses.add_callback(
        responses.GET,
        "https://liquidiction.xyz/api/leaderboard",
        callback=_paged_callback(total=200),
    )
    traders = client.top_traders("7d", n=120)
    assert len(traders) == 120
    assert traders[0].rank == 1
    assert traders[119].rank == 120


@responses.activate
def test_top_traders_handles_empty_response(client: LiquidictionClient):
    responses.add(
        responses.GET,
        "https://liquidiction.xyz/api/leaderboard",
        json={"traders": [], "totalTraders": 0},
    )
    assert client.top_traders("7d", n=10) == []


@responses.activate
def test_top_traders_stops_at_total(client: LiquidictionClient):
    responses.add_callback(
        responses.GET,
        "https://liquidiction.xyz/api/leaderboard",
        callback=_paged_callback(total=5),
    )
    traders = client.top_traders("7d", n=100)
    assert len(traders) == 5


@responses.activate
def test_5xx_propagates(client: LiquidictionClient):
    responses.add(
        responses.GET,
        "https://liquidiction.xyz/api/leaderboard",
        status=503,
    )
    with pytest.raises(requests.HTTPError):
        client.top_traders("7d", n=5)


@responses.activate
def test_malformed_row_uses_defaults(client: LiquidictionClient):
    responses.add(
        responses.GET,
        "https://liquidiction.xyz/api/leaderboard",
        json={"traders": [{"address": "0xABC"}], "totalTraders": 1},
    )
    traders = client.top_traders("7d", n=1)
    assert traders[0].address == "0xabc"
    assert traders[0].rank == -1
    assert traders[0].pnl == 0.0


@responses.activate
def test_address_lowercased(client: LiquidictionClient):
    responses.add(
        responses.GET,
        "https://liquidiction.xyz/api/leaderboard",
        json={
            "traders": [
                {"address": "0x" + "F" * 40, "rank": 1, "pnl": 1, "trades": 1, "volume": 1}
            ],
            "totalTraders": 1,
        },
    )
    t = client.top_traders("7d", n=1)[0]
    assert t.address == ("0x" + "f" * 40)
