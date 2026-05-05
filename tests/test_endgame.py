"""Tests for src/endgame.py — the time-decay capture strategy."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from src.endgame import EndgameConfig, EndgameRunner
from src.errors import OrderError
from src.market_meta import MarketMeta


@pytest.fixture
def eg_cfg() -> EndgameConfig:
    return EndgameConfig(
        target=79980.0,
        expiry_ts=2_000_000_000,  # well in the future for time arithmetic
    )


@pytest.fixture
def market_meta_eg() -> MarketMeta:
    info = MagicMock()
    info.meta.return_value = {"universe": []}
    info.spot_meta.return_value = {"universe": [], "tokens": []}
    mm = MarketMeta(info)
    mm.load()
    return mm


def _runner(eg_cfg, market_meta_eg, journal, dry_run=True):
    info = MagicMock()
    exchange = MagicMock()
    exchange.order.return_value = {"status": "ok"}
    return EndgameRunner(
        info=info,
        exchange=exchange,
        market_meta=market_meta_eg,
        journal=journal,
        config=eg_cfg,
        dry_run=dry_run,
    )


def test_decide_fires_yes_when_btc_above_target_and_yes_priced_low(eg_cfg, market_meta_eg, journal):
    r = _runner(eg_cfg, market_meta_eg, journal)
    state = {"btc": 80_400.0, "yes": 0.85, "no": 0.15}  # cushion +$420, yes 0.85
    decision = r.decide(state, ttl_s=500)  # within T-10min tier
    assert decision == ("#20", 0.90)


def test_decide_fires_no_when_btc_below_target_and_no_priced_low(eg_cfg, market_meta_eg, journal):
    r = _runner(eg_cfg, market_meta_eg, journal)
    state = {"btc": 79_500.0, "yes": 0.15, "no": 0.85}  # cushion -$480, no 0.85
    decision = r.decide(state, ttl_s=500)
    assert decision == ("#21", 0.90)


def test_decide_skips_when_inside_target_band(eg_cfg, market_meta_eg, journal):
    r = _runner(eg_cfg, market_meta_eg, journal)
    state = {"btc": 80_050.0, "yes": 0.55, "no": 0.45}  # cushion +$70 < $400
    assert r.decide(state, ttl_s=500) is None


def test_decide_skips_when_yes_already_high(eg_cfg, market_meta_eg, journal):
    """Big cushion but YES already at 0.95 → no risk premium left, skip."""
    r = _runner(eg_cfg, market_meta_eg, journal)
    state = {"btc": 80_400.0, "yes": 0.95, "no": 0.05}
    # T-10min tier requires yes ≤ 0.90 → fail. T-5min requires ≤ 0.93 → still fail.
    # T-2min requires ≤ 0.96 → would fire here at ttl=100
    assert r.decide(state, ttl_s=500) is None  # only T-10min active, yes>0.90
    assert r.decide(state, ttl_s=200) is None  # T-5min active, yes>0.93
    assert r.decide(state, ttl_s=100) == ("#20", 0.96)  # T-2min active, yes≤0.96 ✓


def test_decide_skips_outside_window(eg_cfg, market_meta_eg, journal):
    """Far from expiry (ttl > 600s = 10min): no tier matches."""
    r = _runner(eg_cfg, market_meta_eg, journal)
    state = {"btc": 81_000.0, "yes": 0.70, "no": 0.30}
    assert r.decide(state, ttl_s=900) is None


def test_decide_each_tier_fires_once(eg_cfg, market_meta_eg, journal):
    r = _runner(eg_cfg, market_meta_eg, journal)
    state = {"btc": 80_400.0, "yes": 0.85, "no": 0.15}
    # Fire T-10min
    assert r.decide(state, ttl_s=500) == ("#20", 0.90)
    r.fired_tiers.add(600)
    # Same state, T-10min already fired → skip
    assert r.decide(state, ttl_s=500) is None
    # T-5min not yet fired → would fire
    assert r.decide(state, ttl_s=200) == ("#20", 0.93)


def test_submit_buy_dry_run_does_not_call_exchange(eg_cfg, market_meta_eg, journal):
    r = _runner(eg_cfg, market_meta_eg, journal, dry_run=True)
    r.info.post.return_value = {"levels": [[], [{"px": "0.85", "sz": "100", "n": 1}]]}
    ok = r.submit_buy("#20", max_px=0.90)
    assert ok is True
    r.exchange.order.assert_not_called()


def test_submit_buy_live_calls_exchange_with_ioc(eg_cfg, market_meta_eg, journal):
    r = _runner(eg_cfg, market_meta_eg, journal, dry_run=False)
    r.info.post.return_value = {"levels": [[], [{"px": "0.85", "sz": "100", "n": 1}]]}
    ok = r.submit_buy("#20", max_px=0.90)
    assert ok is True
    r.exchange.order.assert_called_once()
    args, kwargs = r.exchange.order.call_args
    coin, is_buy, sz, _px = args[:4]
    assert coin == "#20"
    assert is_buy is True
    assert sz == 3.0
    assert kwargs["order_type"] == {"limit": {"tif": "Ioc"}}
    assert kwargs["reduce_only"] is False


def test_submit_buy_skips_when_ask_above_cap(eg_cfg, market_meta_eg, journal):
    r = _runner(eg_cfg, market_meta_eg, journal, dry_run=False)
    r.info.post.return_value = {"levels": [[], [{"px": "0.95", "sz": "100", "n": 1}]]}
    ok = r.submit_buy("#20", max_px=0.90)
    assert ok is False
    r.exchange.order.assert_not_called()


def test_submit_buy_skips_on_sanity_violation(eg_cfg, market_meta_eg, journal):
    r = _runner(eg_cfg, market_meta_eg, journal, dry_run=False)
    # Ask price 0.999 — outside sanity_max_px=0.99
    r.info.post.return_value = {"levels": [[], [{"px": "0.999", "sz": "100", "n": 1}]]}
    ok = r.submit_buy("#20", max_px=0.999)
    assert ok is False
    r.exchange.order.assert_not_called()


def test_submit_buy_skips_when_no_ask(eg_cfg, market_meta_eg, journal):
    r = _runner(eg_cfg, market_meta_eg, journal, dry_run=False)
    r.info.post.return_value = {"levels": [[], []]}  # empty asks
    ok = r.submit_buy("#20", max_px=0.90)
    assert ok is False
    r.exchange.order.assert_not_called()


def test_submit_buy_propagates_exchange_error(eg_cfg, market_meta_eg, journal):
    r = _runner(eg_cfg, market_meta_eg, journal, dry_run=False)
    r.info.post.return_value = {"levels": [[], [{"px": "0.85", "sz": "100", "n": 1}]]}
    r.exchange.order.side_effect = RuntimeError("HL rejected")
    with pytest.raises(OrderError):
        r.submit_buy("#20", max_px=0.90)


def test_kill_switch_blocks_run(eg_cfg, market_meta_eg, journal, tmp_path):
    kill = tmp_path / "KILL"
    kill.touch()
    cfg = EndgameConfig(
        target=eg_cfg.target,
        expiry_ts=eg_cfg.expiry_ts,
        kill_switch_file=str(kill),
    )
    r = _runner(cfg, market_meta_eg, journal)
    # Setting fires=max forces immediate exit if kill switch path matters less,
    # but we want to exercise the kill_switch_active branch — set ttl_s far out
    # so stop_buffer doesn't kick in instead.
    assert r.kill_switch_active() is True
