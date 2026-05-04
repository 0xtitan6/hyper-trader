from unittest.mock import MagicMock

from src.preflight import PreflightReport, format_report, run_preflight


def _info(
    perp=2,
    spot=10,
    outcomes_resp: dict | None = None,
    user_state_ok: bool = True,
    fills: list | None = None,
    fills_exc: Exception | None = None,
):
    info = MagicMock()
    info.base_url = "https://api.hyperliquid-testnet.xyz"
    info.meta.return_value = {"universe": [{"name": f"P{i}"} for i in range(perp)]}
    info.spot_meta.return_value = {"universe": [{"name": f"@{i}"} for i in range(spot)]}
    info.post.return_value = outcomes_resp if outcomes_resp is not None else {"outcomes": []}
    if user_state_ok:
        info.user_state.return_value = {"assetPositions": []}
    else:
        info.user_state.side_effect = RuntimeError("403")
    if fills_exc:
        info.user_fills.side_effect = fills_exc
    else:
        info.user_fills.return_value = fills if fills is not None else []
    return info


def test_healthy_run_with_no_fills_yet():
    info = _info()
    r = run_preflight(info, "0x" + "a" * 40)
    assert r.healthy
    assert r.perp_markets == 2
    assert r.account_reachable
    assert r.schema_ok
    assert r.errors == []


def test_lists_outcomes():
    info = _info(
        outcomes_resp={
            "outcomes": [
                {"outcome": 1, "name": "Recurring", "description": "BTC daily binary"},
            ]
        }
    )
    r = run_preflight(info, "0x" + "a" * 40)
    assert r.outcome_markets == 1
    assert r.outcomes[0]["outcome"] == 1


def test_meta_failure_marks_unhealthy():
    info = _info()
    info.meta.side_effect = RuntimeError("network")
    r = run_preflight(info, "0x" + "a" * 40)
    assert not r.healthy
    assert any("meta" in e for e in r.errors)


def test_user_state_failure_marks_unreachable():
    info = _info(user_state_ok=False)
    r = run_preflight(info, "0x" + "a" * 40)
    assert not r.account_reachable
    assert not r.healthy


def test_fill_schema_validated_against_required_fields():
    valid = {
        "coin": "#11",
        "px": "0.5",
        "sz": "10",
        "side": "B",
        "tid": 1,
        "time": 1714000000000,
        "closedPnl": "0",
        "fee": "0",
    }
    info = _info(fills=[valid])
    r = run_preflight(info, "0x" + "a" * 40)
    assert r.schema_ok
    assert r.schema_missing == []


def test_fill_schema_missing_field_flagged():
    incomplete = {"coin": "#11", "px": "0.5", "sz": "10"}  # missing side, tid, time, etc.
    info = _info(fills=[incomplete])
    r = run_preflight(info, "0x" + "a" * 40)
    assert not r.schema_ok
    assert "side" in r.schema_missing
    assert "tid" in r.schema_missing


def test_fills_fetch_failure_recorded():
    info = _info(fills_exc=RuntimeError("403"))
    r = run_preflight(info, "0x" + "a" * 40)
    assert any("user_fills" in e for e in r.errors)
    assert not r.healthy


def test_outcomes_endpoint_failure_recorded():
    info = _info()
    info.post.side_effect = RuntimeError("404")
    r = run_preflight(info, "0x" + "a" * 40)
    assert any("outcomeMeta" in e for e in r.errors)


def test_format_report_lists_errors_and_outcomes():
    r = PreflightReport(
        api_url="https://api.test",
        account_address="0x" + "a" * 40,
        perp_markets=10,
        spot_markets=5,
        outcome_markets=2,
        outcomes=[{"outcome": 1, "description": "BTC"}, {"outcome": 2, "description": "ETH"}],
        account_reachable=True,
        own_fills_count=3,
        schema_ok=True,
        errors=[],
    )
    out = format_report(r)
    assert "perp markets:    10" in out
    assert "outcome markets: 2" in out
    assert "outcome=1: BTC" in out
    assert "HEALTHY" in out


def test_format_report_unhealthy_shows_errors():
    r = PreflightReport(
        api_url="x",
        account_address="0xa",
        errors=["meta: bad", "user_fills: bad"],
    )
    out = format_report(r)
    assert "ERRORS:" in out
    assert "✗ meta: bad" in out
    assert "UNHEALTHY" in out
