from pathlib import Path
from unittest.mock import MagicMock

import pytest

from src.mirror import MirrorTrader, TradeIntent


@pytest.fixture
def positions():
    p = MagicMock()
    p.realized_pnl_today.return_value = 0.0
    p.total_exposure_usd.return_value = 0.0
    return p


@pytest.fixture
def exchange():
    e = MagicMock()
    e.order.return_value = {"status": "ok"}
    return e


@pytest.fixture
def mt(cfg, exchange, positions, journal, alerter, market_meta):
    return MirrorTrader(cfg, exchange, positions, journal, alerter, market_meta)


def test_happy_path_dry_run_logs_no_order(mt, exchange, outcome_fill):
    mt.on_leader_fill("0xleader", outcome_fill)
    exchange.order.assert_not_called()


def test_happy_path_live_submits(
    cfg, positions, journal, alerter, exchange, outcome_fill, market_meta
):
    cfg = _override_risk(cfg, dry_run=False)
    mt = MirrorTrader(cfg, exchange, positions, journal, alerter, market_meta)
    mt.on_leader_fill("0xleader", outcome_fill)
    exchange.order.assert_called_once()
    args, kwargs = exchange.order.call_args
    coin, is_buy, sz, px = args[:4]
    assert coin == "#11"
    assert is_buy is True
    # 100 sz * 0.54 = 54 leader notional * 0.10 = 5.4 mirror notional / 0.54 = 10 sz
    assert abs(sz - 10.0) < 1e-9
    # IOC slippage: 0.5%
    assert px > float(outcome_fill["px"])  # buy side adds slippage
    assert kwargs["order_type"] == {"limit": {"tif": "Ioc"}}


def test_sell_path_subtracts_slippage(
    cfg, positions, journal, alerter, exchange, outcome_fill, market_meta
):
    cfg = _override_risk(cfg, dry_run=False)
    mt = MirrorTrader(cfg, exchange, positions, journal, alerter, market_meta)
    fill = {**outcome_fill, "side": "A"}
    mt.on_leader_fill("0xleader", fill)
    args, _ = exchange.order.call_args
    px = args[3]
    assert px < float(outcome_fill["px"])  # sell side subtracts


def test_kill_switch_blocks(mt, exchange, cfg, outcome_fill, market_meta):
    Path(cfg.risk.kill_switch_file).touch()
    cfg2 = _override_risk(cfg, dry_run=False)
    mt2 = MirrorTrader(cfg2, exchange, mt.positions, mt.journal, mt.alerter, market_meta)
    mt2.on_leader_fill("0xleader", outcome_fill)
    exchange.order.assert_not_called()


def test_daily_loss_blocks_and_alerts(cfg, positions, journal, exchange, outcome_fill, market_meta):
    positions.realized_pnl_today.return_value = -150.0
    alerter = MagicMock()
    cfg2 = _override_risk(cfg, dry_run=False)
    mt = MirrorTrader(cfg2, exchange, positions, journal, alerter, market_meta)
    mt.on_leader_fill("0xleader", outcome_fill)
    exchange.order.assert_not_called()
    crit = [c for c in alerter.alert.call_args_list if c.args[0] == "critical"]
    assert len(crit) == 1


def test_exposure_cap_blocks(cfg, positions, journal, alerter, exchange, outcome_fill, market_meta):
    positions.total_exposure_usd.return_value = 499.0
    cfg2 = _override_risk(cfg, dry_run=False, max_total_exposure_usd=500)
    mt = MirrorTrader(cfg2, exchange, positions, journal, alerter, market_meta)
    mt.on_leader_fill("0xleader", outcome_fill)
    exchange.order.assert_not_called()


def test_disallowed_market_skipped(cfg, positions, journal, alerter, exchange, market_meta):
    fill = {"tid": 1, "coin": "BTC", "px": "65000", "sz": "0.01", "side": "B"}
    cfg2 = _override_risk(cfg, dry_run=False)  # only outcome allowed by default
    mt = MirrorTrader(cfg2, exchange, positions, journal, alerter, market_meta)
    mt.on_leader_fill("0xleader", fill)
    exchange.order.assert_not_called()


def test_perp_allowed_when_configured(cfg, positions, journal, alerter, exchange, market_meta):
    cfg2 = _override_risk(cfg, dry_run=False, allowed_market_types=["outcome", "perp"])
    mt = MirrorTrader(cfg2, exchange, positions, journal, alerter, market_meta)
    fill = {"tid": 1, "coin": "BTC", "px": "65000", "sz": "0.01", "side": "B"}
    mt.on_leader_fill("0xleader", fill)
    exchange.order.assert_called_once()


def test_below_min_per_trade_skipped(cfg, positions, journal, alerter, exchange, market_meta):
    cfg2 = _override_sizing(cfg, min_per_trade_usd=20)
    cfg2 = _override_risk(cfg2, dry_run=False)
    mt = MirrorTrader(cfg2, exchange, positions, journal, alerter, market_meta)
    # leader_notional = 1 * 0.01 = 0.01, * 0.10 = 0.001 → below min
    fill = {"tid": 1, "coin": "#11", "px": "0.01", "sz": "1", "side": "B"}
    mt.on_leader_fill("0xleader", fill)
    exchange.order.assert_not_called()


def test_max_per_trade_caps_size(cfg, positions, journal, alerter, exchange, market_meta):
    cfg2 = _override_sizing(cfg, max_per_trade_usd=50)
    cfg2 = _override_risk(cfg2, dry_run=False)
    mt = MirrorTrader(cfg2, exchange, positions, journal, alerter, market_meta)
    # huge leader fill: $10000 notional * 0.1 = $1000, capped at $50
    fill = {"tid": 1, "coin": "#11", "px": "1.00", "sz": "10000", "side": "B"}
    mt.on_leader_fill("0xleader", fill)
    args, _ = exchange.order.call_args
    sz = args[2]
    assert sz == 50.0  # 50 / 1.00 = 50


def test_fixed_sizing_uses_fixed_usd(cfg, positions, journal, alerter, exchange, market_meta):
    cfg2 = _override_sizing(cfg, mode="fixed", fixed_usd=30)
    cfg2 = _override_risk(cfg2, dry_run=False)
    mt = MirrorTrader(cfg2, exchange, positions, journal, alerter, market_meta)
    fill = {"tid": 1, "coin": "#11", "px": "0.50", "sz": "1000", "side": "B"}
    mt.on_leader_fill("0xleader", fill)
    args, _ = exchange.order.call_args
    sz = args[2]
    assert sz == 60.0  # 30 / 0.50


def test_malformed_fills_skipped(mt, exchange):
    bad_fills = [
        {"tid": 1},  # missing everything
        {"tid": 1, "coin": "#11", "px": "0", "sz": "10", "side": "B"},  # zero px
        {"tid": 1, "coin": "#11", "px": "0.5", "sz": "0", "side": "B"},  # zero sz
        {"tid": 1, "coin": "#11", "px": "0.5", "sz": "10", "side": "?"},  # bad side
        {"tid": 1, "coin": "", "px": "0.5", "sz": "10", "side": "B"},  # empty coin
        {"tid": 1, "coin": "#11", "px": "abc", "sz": "10", "side": "B"},  # non-numeric
    ]
    for f in bad_fills:
        mt.on_leader_fill("0xleader", f)
    exchange.order.assert_not_called()


def test_order_failure_alerts_and_propagates(
    cfg, positions, journal, exchange, outcome_fill, market_meta
):
    from src.errors import OrderError

    cfg2 = _override_risk(cfg, dry_run=False)
    exchange.order.side_effect = RuntimeError("nonce too low")
    alerter = MagicMock()
    mt = MirrorTrader(cfg2, exchange, positions, journal, alerter, market_meta)
    with pytest.raises(OrderError):
        mt.on_leader_fill("0xleader", outcome_fill)
    err = [c for c in alerter.alert.call_args_list if c.args[0] == "error"]
    assert any("Order submit failed" in c.args[1] for c in err)


def test_journal_records_decisions(
    cfg, positions, exchange, alerter, outcome_fill, tmp_path, market_meta
):
    import json as _json

    from src.journal import Journal as J

    j = J(str(tmp_path / "j.jsonl"))
    cfg2 = _override_risk(cfg, dry_run=True)
    mt = MirrorTrader(cfg2, exchange, positions, j, alerter, market_meta)
    mt.on_leader_fill("0xleader", outcome_fill)
    lines = (tmp_path / "j.jsonl").read_text().splitlines()
    events = [_json.loads(ln)["event"] for ln in lines]
    assert "leader_fill" in events
    assert "risk_check" in events
    assert "order_dry_run" in events


def _override_risk(cfg, **changes):
    from dataclasses import replace

    return replace(cfg, risk=replace(cfg.risk, **changes))


def _override_sizing(cfg, **changes):
    from dataclasses import replace

    return replace(cfg, sizing=replace(cfg.sizing, **changes))


def test_intent_dataclass_basic():
    i = TradeIntent(coin="#11", is_buy=True, sz=10.0, limit_px=0.5, notional_usd=5.0)
    assert i.coin == "#11" and i.is_buy is True
