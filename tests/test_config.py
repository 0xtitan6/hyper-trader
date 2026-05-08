import dataclasses
from pathlib import Path

import pytest
import yaml

from src.config import load_config


def _base_yaml() -> dict:
    return {
        "discovery": {
            "period": "7d",
            "top_n": 3,
            "min_trades": 10,
            "min_volume_usd": 100,
            "min_pnl_usd": 10,
            "refresh_seconds": 600,
        },
        "sizing": {
            "mode": "proportional",
            "proportional_fraction": 0.1,
            "fixed_usd": 25,
            "max_per_trade_usd": 100,
            "min_per_trade_usd": 5,
        },
        "risk": {
            "dry_run": True,
            "max_total_exposure_usd": 500,
            "max_daily_loss_usd": 100,
            "allowed_market_types": ["outcome"],
            "kill_switch_file": "./KILL",
        },
        "network": {"hyperliquid_env": "testnet", "liquidiction_base": "https://liquidiction.xyz"},
        "ops": {"state_db": "./s.db", "journal_path": "./j.jsonl"},
    }


def _write_yaml(tmp_path: Path, data: dict) -> str:
    p = tmp_path / "config.yaml"
    p.write_text(yaml.safe_dump(data))
    return str(p)


@pytest.fixture
def good_env() -> dict[str, str]:
    return {
        "HL_PRIVATE_KEY": "0x" + "1" * 64,
        "HL_ACCOUNT_ADDRESS": "0x" + "a" * 40,
        "HL_NETWORK": "testnet",
    }


def test_loads_valid_config(tmp_path, good_env):
    cfg = load_config(_write_yaml(tmp_path, _base_yaml()), env=good_env)
    assert cfg.network.hyperliquid_env == "testnet"
    assert cfg.hyperliquid_api_url == "https://api.hyperliquid-testnet.xyz"
    assert cfg.discovery.top_n == 3
    assert cfg.sizing.mode == "proportional"
    assert cfg.ops.log_level == "INFO"
    assert cfg.webhook_url == ""


def test_mainnet_url(tmp_path, good_env):
    raw = _base_yaml()
    raw["network"]["hyperliquid_env"] = "mainnet"
    cfg = load_config(_write_yaml(tmp_path, raw), env={**good_env, "HL_NETWORK": "mainnet"})
    assert cfg.hyperliquid_api_url == "https://api.hyperliquid.xyz"


def test_webhook_url_picked_up(tmp_path, good_env):
    cfg = load_config(
        _write_yaml(tmp_path, _base_yaml()),
        env={**good_env, "ALERT_WEBHOOK_URL": "https://hooks.slack.example/x"},
    )
    assert cfg.webhook_url == "https://hooks.slack.example/x"


def test_missing_private_key(tmp_path, good_env):
    env = {**good_env, "HL_PRIVATE_KEY": ""}
    with pytest.raises(SystemExit, match="HL_PRIVATE_KEY"):
        load_config(_write_yaml(tmp_path, _base_yaml()), env=env)


def test_placeholder_private_key(tmp_path, good_env):
    env = {**good_env, "HL_PRIVATE_KEY": "0x" + "0" * 64}
    with pytest.raises(SystemExit, match="HL_PRIVATE_KEY"):
        load_config(_write_yaml(tmp_path, _base_yaml()), env=env)


def test_missing_account_address(tmp_path, good_env):
    env = {**good_env, "HL_ACCOUNT_ADDRESS": ""}
    with pytest.raises(SystemExit, match="HL_ACCOUNT_ADDRESS"):
        load_config(_write_yaml(tmp_path, _base_yaml()), env=env)


def test_invalid_network_env(tmp_path, good_env):
    env = {**good_env, "HL_NETWORK": "devnet"}
    with pytest.raises(SystemExit, match="HL_NETWORK"):
        load_config(_write_yaml(tmp_path, _base_yaml()), env=env)


def test_network_mismatch_refuses(tmp_path, good_env):
    env = {**good_env, "HL_NETWORK": "mainnet"}
    # YAML says testnet, env says mainnet
    with pytest.raises(SystemExit, match="mismatch"):
        load_config(_write_yaml(tmp_path, _base_yaml()), env=env)


def test_missing_yaml(tmp_path, good_env):
    with pytest.raises(SystemExit, match="not found"):
        load_config(str(tmp_path / "missing.yaml"), env=good_env)


def test_yaml_not_a_mapping(tmp_path, good_env):
    p = tmp_path / "config.yaml"
    p.write_text("- just\n- a\n- list\n")
    with pytest.raises(SystemExit, match="not a mapping"):
        load_config(str(p), env=good_env)


def test_invalid_sizing_mode(tmp_path, good_env):
    raw = _base_yaml()
    raw["sizing"]["mode"] = "weird"
    with pytest.raises(SystemExit, match=r"sizing\.mode"):
        load_config(_write_yaml(tmp_path, raw), env=good_env)


def test_min_above_max_size(tmp_path, good_env):
    raw = _base_yaml()
    raw["sizing"]["min_per_trade_usd"] = 200
    with pytest.raises(SystemExit, match="min_per_trade_usd"):
        load_config(_write_yaml(tmp_path, raw), env=good_env)


def test_outcome_min_negative_rejected(tmp_path, good_env):
    raw = _base_yaml()
    raw["sizing"]["outcome_min_per_trade_usd"] = -5
    with pytest.raises(SystemExit, match="outcome_min_per_trade_usd"):
        load_config(_write_yaml(tmp_path, raw), env=good_env)


def test_outcome_min_above_max_rejected(tmp_path, good_env):
    raw = _base_yaml()
    raw["sizing"]["outcome_min_per_trade_usd"] = 999  # > max_per_trade_usd
    with pytest.raises(SystemExit, match="outcome_min_per_trade_usd"):
        load_config(_write_yaml(tmp_path, raw), env=good_env)


def test_outcome_min_optional(tmp_path, good_env):
    """Omitting outcome_min_per_trade_usd loads cleanly (falls back to global min)."""
    raw = _base_yaml()
    raw["sizing"].pop("outcome_min_per_trade_usd", None)
    cfg = load_config(_write_yaml(tmp_path, raw), env=good_env)
    assert cfg.sizing.outcome_min_per_trade_usd is None


def test_invalid_market_type(tmp_path, good_env):
    raw = _base_yaml()
    raw["risk"]["allowed_market_types"] = ["bogus"]
    with pytest.raises(SystemExit, match="allowed_market_types"):
        load_config(_write_yaml(tmp_path, raw), env=good_env)


def test_empty_market_types(tmp_path, good_env):
    raw = _base_yaml()
    raw["risk"]["allowed_market_types"] = []
    with pytest.raises(SystemExit, match="at least one"):
        load_config(_write_yaml(tmp_path, raw), env=good_env)


def test_top_n_too_high(tmp_path, good_env):
    # 10 used to be allowed; now refused because own-fill sub takes 1 of HL's 10/IP cap
    raw = _base_yaml()
    raw["discovery"]["top_n"] = 10
    with pytest.raises(SystemExit, match="top_n"):
        load_config(_write_yaml(tmp_path, raw), env=good_env)


def test_top_n_at_cap_ok(tmp_path, good_env):
    raw = _base_yaml()
    raw["discovery"]["top_n"] = 9
    cfg = load_config(_write_yaml(tmp_path, raw), env=good_env)
    assert cfg.discovery.top_n == 9


def test_top_n_zero(tmp_path, good_env):
    raw = _base_yaml()
    raw["discovery"]["top_n"] = 0
    with pytest.raises(SystemExit, match="top_n"):
        load_config(_write_yaml(tmp_path, raw), env=good_env)


def test_invalid_ioc_slippage_bps(tmp_path, good_env):
    raw = _base_yaml()
    raw["sizing"]["ioc_slippage_bps"] = 1500  # > 1000
    with pytest.raises(SystemExit, match="ioc_slippage_bps"):
        load_config(_write_yaml(tmp_path, raw), env=good_env)


def test_always_follow_valid(tmp_path, good_env):
    raw = _base_yaml()
    raw["discovery"]["always_follow"] = ["0x" + "b" * 40]
    cfg = load_config(_write_yaml(tmp_path, raw), env=good_env)
    assert cfg.discovery.always_follow == ["0x" + "b" * 40]


def test_always_follow_rejects_short_address(tmp_path, good_env):
    raw = _base_yaml()
    raw["discovery"]["always_follow"] = ["0xabc"]  # too short
    with pytest.raises(SystemExit, match="always_follow"):
        load_config(_write_yaml(tmp_path, raw), env=good_env)


def test_always_follow_rejects_non_hex(tmp_path, good_env):
    raw = _base_yaml()
    raw["discovery"]["always_follow"] = ["0x" + "z" * 40]  # bad hex
    with pytest.raises(SystemExit, match="always_follow"):
        load_config(_write_yaml(tmp_path, raw), env=good_env)


def test_always_follow_rejects_missing_0x(tmp_path, good_env):
    raw = _base_yaml()
    raw["discovery"]["always_follow"] = ["b" * 42]
    with pytest.raises(SystemExit, match="always_follow"):
        load_config(_write_yaml(tmp_path, raw), env=good_env)


def test_top_n_plus_always_follow_exceeds_ws_cap(tmp_path, good_env):
    """top_n=8 + 2 always-follow = 10 — exceeds the 9-sub headroom (own_fills takes 1)."""
    raw = _base_yaml()
    raw["discovery"]["top_n"] = 8
    raw["discovery"]["always_follow"] = ["0x" + "b" * 40, "0x" + "c" * 40]
    with pytest.raises(SystemExit, match="exceeds HL"):
        load_config(_write_yaml(tmp_path, raw), env=good_env)


def test_top_n_plus_always_follow_at_cap_ok(tmp_path, good_env):
    """top_n=7 + 2 always-follow = 9 — OK, hits the cap exactly."""
    raw = _base_yaml()
    raw["discovery"]["top_n"] = 7
    raw["discovery"]["always_follow"] = ["0x" + "b" * 40, "0x" + "c" * 40]
    cfg = load_config(_write_yaml(tmp_path, raw), env=good_env)
    assert cfg.discovery.top_n == 7
    assert len(cfg.discovery.always_follow) == 2


def test_always_follow_does_not_clobber_account_address(tmp_path, good_env):
    """Regression: a previous version of the always_follow validator used
    `addr` as both the env-var lookup *and* the loop variable, so the
    final account_address ended up being the last always_follow entry —
    rerouting bot ops to a leader's wallet. The fix renamed the loop var.
    """
    raw = _base_yaml()
    raw["discovery"]["always_follow"] = [
        "0x" + "b" * 40,
        "0x" + "c" * 40,
        "0x" + "d" * 40,
    ]
    cfg = load_config(_write_yaml(tmp_path, raw), env=good_env)
    assert cfg.account_address == good_env["HL_ACCOUNT_ADDRESS"]
    assert cfg.account_address != "0x" + "d" * 40


def test_leader_weights_valid(tmp_path, good_env):
    raw = _base_yaml()
    raw["discovery"]["leader_weights"] = {"0x" + "b" * 40: 2.0}
    cfg = load_config(_write_yaml(tmp_path, raw), env=good_env)
    assert cfg.discovery.leader_weights == {"0x" + "b" * 40: 2.0}


def test_leader_weights_rejects_bad_address(tmp_path, good_env):
    raw = _base_yaml()
    raw["discovery"]["leader_weights"] = {"not-an-addr": 2.0}
    with pytest.raises(SystemExit, match="leader_weights"):
        load_config(_write_yaml(tmp_path, raw), env=good_env)


def test_leader_weights_rejects_out_of_range(tmp_path, good_env):
    raw = _base_yaml()
    raw["discovery"]["leader_weights"] = {"0x" + "b" * 40: 10.0}  # > 5.0 cap
    with pytest.raises(SystemExit, match="leader_weights"):
        load_config(_write_yaml(tmp_path, raw), env=good_env)


def test_leader_weights_rejects_negative(tmp_path, good_env):
    raw = _base_yaml()
    raw["discovery"]["leader_weights"] = {"0x" + "b" * 40: -1.0}
    with pytest.raises(SystemExit, match="leader_weights"):
        load_config(_write_yaml(tmp_path, raw), env=good_env)


def test_negative_ioc_slippage_bps(tmp_path, good_env):
    raw = _base_yaml()
    raw["sizing"]["ioc_slippage_bps"] = -1
    with pytest.raises(SystemExit, match="ioc_slippage_bps"):
        load_config(_write_yaml(tmp_path, raw), env=good_env)


def test_ioc_slippage_bps_default(tmp_path, good_env):
    raw = _base_yaml()
    # not setting ioc_slippage_bps — should default to 50
    cfg = load_config(_write_yaml(tmp_path, raw), env=good_env)
    assert cfg.sizing.ioc_slippage_bps == 50.0


def test_invalid_period(tmp_path, good_env):
    raw = _base_yaml()
    raw["discovery"]["period"] = "1d"
    with pytest.raises(SystemExit, match="period"):
        load_config(_write_yaml(tmp_path, raw), env=good_env)


def test_invalid_log_level(tmp_path, good_env):
    raw = _base_yaml()
    raw["ops"]["log_level"] = "TRACE"
    with pytest.raises(SystemExit, match="log_level"):
        load_config(_write_yaml(tmp_path, raw), env=good_env)


def test_invalid_alert_level(tmp_path, good_env):
    raw = _base_yaml()
    raw["ops"]["alert_min_level"] = "panic"
    with pytest.raises(SystemExit, match="alert_min_level"):
        load_config(_write_yaml(tmp_path, raw), env=good_env)


def test_ops_section_optional(tmp_path, good_env):
    raw = _base_yaml()
    del raw["ops"]
    cfg = load_config(_write_yaml(tmp_path, raw), env=good_env)
    assert cfg.ops.log_level == "INFO"
    assert cfg.ops.log_json is False


def test_config_is_immutable(tmp_path, good_env):
    cfg = load_config(_write_yaml(tmp_path, _base_yaml()), env=good_env)
    with pytest.raises((AttributeError, dataclasses.FrozenInstanceError)):
        cfg.network.hyperliquid_env = "mainnet"  # type: ignore[misc]
