from pathlib import Path

import pytest

from src.alerts import NullAlerter
from src.config import (
    Config,
    DiscoveryConfig,
    NetworkConfig,
    OpsConfig,
    RiskConfig,
    SizingConfig,
)
from src.journal import Journal
from src.state import State


@pytest.fixture
def state(tmp_path: Path) -> State:
    return State(str(tmp_path / "state.db"))


@pytest.fixture
def journal(tmp_path: Path) -> Journal:
    return Journal(str(tmp_path / "journal.jsonl"))


@pytest.fixture
def alerter() -> NullAlerter:
    return NullAlerter()


@pytest.fixture
def cfg(tmp_path: Path) -> Config:
    return Config(
        discovery=DiscoveryConfig(
            period="7d",
            top_n=3,
            min_trades=10,
            min_volume_usd=100.0,
            min_pnl_usd=10.0,
            refresh_seconds=600,
        ),
        sizing=SizingConfig(
            mode="proportional",
            proportional_fraction=0.10,
            fixed_usd=25.0,
            max_per_trade_usd=100.0,
            min_per_trade_usd=5.0,
        ),
        risk=RiskConfig(
            dry_run=True,
            max_total_exposure_usd=500.0,
            max_daily_loss_usd=100.0,
            allowed_market_types=["outcome"],
            kill_switch_file=str(tmp_path / "KILL"),
        ),
        network=NetworkConfig(
            hyperliquid_env="testnet", liquidiction_base="https://liquidiction.xyz"
        ),
        ops=OpsConfig(
            state_db=str(tmp_path / "state.db"),
            journal_path=str(tmp_path / "j.jsonl"),
            log_level="INFO",
            log_json=False,
            ws_stale_threshold_s=120.0,
            alert_min_level="warn",
        ),
        private_key="0x" + "1" * 64,
        account_address="0x" + "a" * 40,
        webhook_url="",
    )


@pytest.fixture
def outcome_fill() -> dict:
    return {
        "tid": 1001,
        "coin": "#11",
        "px": "0.54",
        "sz": "100",
        "side": "B",
        "time": 1714000000000,
        "fee": "0.05",
        "closedPnl": "0",
    }
