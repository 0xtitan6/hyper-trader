"""End-to-end integration test with mocked exchange + leaderboard.

Exercises the full pipeline: discovery → follower subscription → fill arrival
→ mirror sizing → risk check → dry-run journal entry.
"""

import json
from dataclasses import replace
from pathlib import Path
from unittest.mock import MagicMock

import responses

from src.alerts import NullAlerter
from src.follower import FillFollower
from src.journal import Journal
from src.leaders import discover_leaders
from src.liquidiction import LiquidictionClient
from src.mirror import MirrorTrader
from src.positions import PositionTracker
from src.state import State


@responses.activate
def test_full_pipeline_dry_run(cfg, market_meta, tmp_path: Path):
    # 1. Mock leaderboard
    responses.add(
        responses.GET,
        "https://liquidiction.xyz/api/leaderboard",
        json={
            "traders": [
                {
                    "address": "0x" + "A" * 40,
                    "rank": 1,
                    "pnl": 5000,
                    "trades": 200,
                    "volume": 50_000,
                },
                {
                    "address": "0x" + "B" * 40,
                    "rank": 2,
                    "pnl": 1500,
                    "trades": 50,
                    "volume": 10_000,
                },
            ],
            "totalTraders": 2,
        },
    )

    # 2. Wire dependencies
    state = State(str(tmp_path / "state.db"))
    journal = Journal(str(tmp_path / "j.jsonl"))
    alerter = NullAlerter()

    captured_subs: list[dict] = []
    captured_cbs: list = []
    info = MagicMock()

    def subscribe(sub, cb):
        captured_subs.append(sub)
        captured_cbs.append(cb)

    info.subscribe.side_effect = subscribe

    exchange = MagicMock()

    # 3. Discover leaders
    liq = LiquidictionClient(cfg.network.liquidiction_base)
    leaders = discover_leaders(liq, cfg.discovery)
    assert len(leaders) == 2

    # 4. Start trackers and followers
    positions = PositionTracker(info, cfg.account_address, state, journal)
    positions.start()

    mirror = MirrorTrader(cfg, exchange, positions, journal, alerter, market_meta)
    follower = FillFollower(info, mirror.on_leader_fill, state)
    follower.follow([t.address for t in leaders])

    # Should have subscribed: 1 own + 2 leaders = 3 user subs
    assert info.subscribe.call_count == 3

    # 5. Find the leader 0xaaa... callback (skip the first which is own-fills)
    leader_a_addr = leaders[0].address
    leader_a_cb = None
    for sub, cb in zip(captured_subs[1:], captured_cbs[1:], strict=False):
        if sub["user"] == leader_a_addr:
            leader_a_cb = cb
            break
    assert leader_a_cb is not None

    # 6. Send a snapshot first (should NOT trigger order)
    leader_a_cb(
        {
            "data": {
                "isSnapshot": True,
                "fills": [
                    {"tid": 1, "coin": "#11", "px": "0.5", "sz": "100", "side": "B"},
                ],
            }
        }
    )
    assert exchange.order.call_count == 0

    # 7. Now a real fill — should mirror under dry_run
    leader_a_cb(
        {
            "data": {
                "isSnapshot": False,
                "fills": [
                    {
                        "tid": 2,
                        "coin": "#11",
                        "px": "0.50",
                        "sz": "100",
                        "side": "B",
                        "time": 1714000000000,
                        "fee": "0.05",
                        "closedPnl": "0",
                    },
                ],
            }
        }
    )

    # dry_run, so no real order
    assert exchange.order.call_count == 0

    # 8. Journal should have leader_fill, risk_check, order_dry_run
    lines = (tmp_path / "j.jsonl").read_text().splitlines()
    events = [json.loads(ln)["event"] for ln in lines]
    assert "leader_fill" in events
    assert "risk_check" in events
    assert "order_dry_run" in events

    # 9. Snapshot tids should be marked seen — replay does nothing
    leader_a_cb(
        {
            "data": {
                "isSnapshot": False,
                "fills": [
                    {"tid": 1, "coin": "#11", "px": "0.5", "sz": "100", "side": "B"},
                ],
            }
        }
    )
    # still no order; tid 1 was deduped
    assert exchange.order.call_count == 0


@responses.activate
def test_pipeline_with_live_order_submission(cfg, market_meta, tmp_path: Path):
    cfg = replace(cfg, risk=replace(cfg.risk, dry_run=False))
    responses.add(
        responses.GET,
        "https://liquidiction.xyz/api/leaderboard",
        json={
            "traders": [
                {
                    "address": "0x" + "1" * 40,
                    "rank": 1,
                    "pnl": 5000,
                    "trades": 200,
                    "volume": 50_000,
                },
            ],
            "totalTraders": 1,
        },
    )

    state = State(str(tmp_path / "s.db"))
    journal = Journal(str(tmp_path / "j.jsonl"))
    info = MagicMock()
    captured: list = []
    info.subscribe.side_effect = lambda s, c: captured.append((s, c))
    exchange = MagicMock()
    exchange.order.return_value = {"status": "ok", "response": {"oid": 12345}}

    leaders = discover_leaders(LiquidictionClient(cfg.network.liquidiction_base), cfg.discovery)
    positions = PositionTracker(info, cfg.account_address, state, journal)
    positions.start()
    mirror = MirrorTrader(cfg, exchange, positions, journal, NullAlerter(), market_meta)
    follower = FillFollower(info, mirror.on_leader_fill, state)
    follower.follow([t.address for t in leaders])

    leader_cb = next(
        cb
        for sub, cb in zip([s for s, _ in captured], [c for _, c in captured], strict=True)
        if sub["user"] == leaders[0].address
    )
    leader_cb(
        {
            "data": {
                "isSnapshot": False,
                "fills": [
                    {
                        "tid": 100,
                        "coin": "#11",
                        "px": "0.50",
                        "sz": "100",
                        "side": "B",
                        "time": 1714000000000,
                        "fee": "0",
                        "closedPnl": "0",
                    },
                ],
            }
        }
    )

    exchange.order.assert_called_once()
    args, kwargs = exchange.order.call_args
    assert args[0] == "#11"
    assert args[1] is True  # buy
    # leader notional = 50; mirror notional = 5; sz = 5 / 0.5 = 10
    assert abs(args[2] - 10.0) < 1e-9
    assert kwargs["order_type"] == {"limit": {"tif": "Ioc"}}
    assert kwargs["reduce_only"] is False  # no opposing position
